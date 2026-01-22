"""
Mail Automation Engine - Production Server

Multi-user Gmail monitoring with JWT authentication.
"""

import os
import json
import base64
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional, List

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

from models import User, Email, get_next_user_id, get_db, SessionLocal
from services.auth import (
    create_tokens, 
    verify_token, 
    get_current_user,
    create_access_token,
    create_refresh_token
)

# Gmail OAuth Scopes
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send"
]

# OAuth config from environment variables
CLIENT_CONFIG = {
    "web": {
        "client_id": os.getenv("CLIENT_ID"),
        "project_id": os.getenv("PROJECT_ID"),
        "auth_uri": os.getenv("AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
        "token_uri": os.getenv("TOKEN_URI", "https://oauth2.googleapis.com/token"),
        "auth_provider_x509_cert_url": os.getenv("AUTH_PROVIDER_X509_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
        "client_secret": os.getenv("CLIENT_SECRET"),
        "redirect_uris": [os.getenv("REDIRECT_URI", "http://localhost:8000/auth/google/callback")]
    }
}

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "mail-automation-engine")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")

app = FastAPI(title="Mail Automation Engine")
templates = Jinja2Templates(directory="templates")


# ==================== UI Routes ====================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Landing page with Connect Gmail button."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """User dashboard (requires auth via frontend)."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ==================== OAuth Routes ====================

@app.get("/auth/google")
def auth_google():
    """Initiate Google OAuth flow."""
    redirect_uri = os.getenv("REDIRECT_URI", "http://localhost:8000/auth/google/callback")
    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent"
    )
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def auth_callback(code: str):
    """
    Handle OAuth callback.
    - Check if user exists, create if not
    - Issue JWT tokens
    - Redirect to dashboard with tokens
    """
    redirect_uri = os.getenv("REDIRECT_URI", "http://localhost:8000/auth/google/callback")
    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    flow.fetch_token(code=code)
    creds = flow.credentials

    # Get user profile from Gmail
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    email = profile["emailAddress"]
    history_id = profile["historyId"]

    # Database operations
    db = SessionLocal()
    try:
        # Check if user exists
        user = db.query(User).filter(User.email_id == email).first()

        if user:
            # Update existing user's OAuth tokens
            user.token = creds.token
            user.refresh_token = creds.refresh_token or user.refresh_token
            user.expiry = creds.expiry
            user.history_id = history_id
            print(f"🔄 Updated existing user: {email}")
        else:
            # Create new user
            user_id = get_next_user_id(db)
            user = User(
                user_id=user_id,
                email_id=email,
                token=creds.token,
                refresh_token=creds.refresh_token,
                token_uri=creds.token_uri,
                client_id=creds.client_id,
                client_secret=creds.client_secret,
                scopes=list(creds.scopes) if creds.scopes else SCOPES,
                universe_domain=getattr(creds, 'universe_domain', 'googleapis.com'),
                expiry=creds.expiry,
                history_id=history_id,
                monitoring_status=False
            )
            db.add(user)
            print(f"✨ Created new user: {user_id} - {email}")

        db.commit()
        db.refresh(user)

        # Create JWT tokens
        tokens = create_tokens(user.user_id, email)

        # Redirect to dashboard with tokens as query params
        # (Frontend will store these and use for API calls)
        return RedirectResponse(
            f"{FRONTEND_URL}/dashboard?access_token={tokens['access_token']}&refresh_token={tokens['refresh_token']}&user_id={user.user_id}"
        )

    except Exception as e:
        db.rollback()
        print(f"❌ Error in OAuth callback: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ==================== Auth API Routes ====================

class RefreshRequest(BaseModel):
    refresh_token: str


@app.post("/auth/refresh")
def refresh_tokens(request: RefreshRequest):
    """Refresh access token using refresh token."""
    try:
        payload = verify_token(request.refresh_token, token_type="refresh")
        new_tokens = create_tokens(payload["user_id"], payload["email"])
        return new_tokens
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


@app.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    """Get current authenticated user info."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user.to_dict()
    finally:
        db.close()


# ==================== Watch Control Routes ====================

@app.post("/watch/start")
def start_watch(current_user: dict = Depends(get_current_user)):
    """Start Gmail watch for the authenticated user."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Build Gmail credentials from stored tokens
        creds = Credentials(
            token=user.token,
            refresh_token=user.refresh_token,
            token_uri=user.token_uri or "https://oauth2.googleapis.com/token",
            client_id=user.client_id or os.getenv("CLIENT_ID"),
            client_secret=user.client_secret or os.getenv("CLIENT_SECRET"),
            scopes=user.scopes or SCOPES
        )

        # Refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            user.token = creds.token
            user.expiry = creds.expiry

        # Start Gmail watch
        service = build("gmail", "v1", credentials=creds)
        watch_response = service.users().watch(
            userId="me",
            body={
                "labelIds": ["INBOX"],
                "topicName": f"projects/{GCP_PROJECT_ID}/topics/gmail-inbox-events"
            }
        ).execute()

        # Update user record
        user.monitoring_status = True
        user.history_id = str(watch_response.get("historyId", user.history_id))
        
        # Watch expires after 7 days
        expiration_ms = int(watch_response.get("expiration", 0))
        if expiration_ms:
            user.last_watch_expiry = datetime.fromtimestamp(expiration_ms / 1000)

        db.commit()

        print(f"▶️ Started watching: {user.email_id}")
        return {
            "status": "success",
            "message": f"Started watching {user.email_id}",
            "history_id": user.history_id,
            "expiration": user.last_watch_expiry.isoformat() if user.last_watch_expiry else None
        }

    except Exception as e:
        db.rollback()
        print(f"❌ Error starting watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/watch/stop")
def stop_watch(current_user: dict = Depends(get_current_user)):
    """Stop Gmail watch for the authenticated user."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Build Gmail credentials
        creds = Credentials(
            token=user.token,
            refresh_token=user.refresh_token,
            token_uri=user.token_uri or "https://oauth2.googleapis.com/token",
            client_id=user.client_id or os.getenv("CLIENT_ID"),
            client_secret=user.client_secret or os.getenv("CLIENT_SECRET"),
            scopes=user.scopes or SCOPES
        )

        # Refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            user.token = creds.token
            user.expiry = creds.expiry

        # Stop Gmail watch
        service = build("gmail", "v1", credentials=creds)
        service.users().stop(userId="me").execute()

        # Update user record
        user.monitoring_status = False
        db.commit()

        print(f"⏹️ Stopped watching: {user.email_id}")
        return {
            "status": "success",
            "message": f"Stopped watching {user.email_id}"
        }

    except Exception as e:
        db.rollback()
        print(f"❌ Error stopping watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ==================== Gmail Webhook ====================

@app.post("/webhooks/gmail")
async def gmail_push(request: Request):
    """Handle Gmail Pub/Sub push notifications."""
    body = await request.json()

    data = body["message"]["data"]
    payload = json.loads(base64.b64decode(data).decode())
    print("🔥 WEBHOOK HIT 🔥")
    print(f"Payload: {payload}")

    email_address = payload["emailAddress"]
    new_history_id = payload["historyId"]

    db = SessionLocal()
    try:
        # Find user by email
        user = db.query(User).filter(User.email_id == email_address).first()
        
        if not user:
            print(f"⚠️ No user found for {email_address}")
            return {"status": "ok", "message": "User not found"}

        # Check if monitoring is enabled
        if not user.monitoring_status:
            print(f"⏸️ Monitoring disabled for {email_address}, skipping")
            return {"status": "ok", "message": "Monitoring disabled"}

        last_history_id = user.history_id
        if not last_history_id:
            print(f"⚠️ No stored history ID for {email_address}. Storing and waiting.")
            user.history_id = str(new_history_id)
            db.commit()
            return {"status": "ok", "message": "Stored initial history ID"}

        print(f"📧 Querying history from {last_history_id} (webhook gave {new_history_id})")

        # Build credentials
        creds = Credentials(
            token=user.token,
            refresh_token=user.refresh_token,
            token_uri=user.token_uri or "https://oauth2.googleapis.com/token",
            client_id=user.client_id or os.getenv("CLIENT_ID"),
            client_secret=user.client_secret or os.getenv("CLIENT_SECRET"),
            scopes=user.scopes or SCOPES
        )

        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            user.token = creds.token
            user.expiry = creds.expiry

        service = build("gmail", "v1", credentials=creds)

        history = service.users().history().list(
            userId="me",
            startHistoryId=last_history_id,
            historyTypes=["messageAdded"]
        ).execute()

        print(f"📬 History response: {history}")

        # Update history ID
        api_history_id = int(history.get("historyId", new_history_id))
        latest_history_id = max(api_history_id, int(new_history_id), int(last_history_id))
        user.history_id = str(latest_history_id)

        # Get existing email IDs for this user
        existing_ids = {e.id for e in db.query(Email.id).filter(Email.user_id == user.user_id).all()}

        emails_added = 0
        for h in history.get("history", []):
            for m in h.get("messagesAdded", []):
                msg_id = m["message"]["id"]
                if msg_id in existing_ids:
                    continue

                try:
                    msg = service.users().messages().get(
                        userId="me",
                        id=msg_id,
                        format="full"
                    ).execute()
                except Exception as fetch_error:
                    print(f"⚠️ Skipping message {msg_id}: {fetch_error}")
                    continue

                headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                body_text = extract_body(msg["payload"])

                # Store email in database
                email_record = Email(
                    id=msg_id,
                    user_id=user.user_id,
                    from_address=headers.get("From"),
                    subject=headers.get("Subject"),
                    body=body_text,
                    received_at=datetime.utcnow()
                )
                db.add(email_record)
                emails_added += 1
                print(f"✅ Stored: {headers.get('Subject')}")

        db.commit()
        return {"status": "ok", "emails_added": emails_added, "history_id": latest_history_id}

    except Exception as e:
        db.rollback()
        print(f"❌ Webhook error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


def extract_body(payload_data) -> str:
    """Extract email body from Gmail message payload."""
    def extract_from_parts(parts):
        plain_text = ""
        html_text = ""
        
        for part in parts:
            mime_type = part.get("mimeType", "")
            
            if mime_type.startswith("multipart/") and "parts" in part:
                nested_plain, nested_html = extract_from_parts(part["parts"])
                if nested_plain:
                    plain_text = nested_plain
                if nested_html:
                    html_text = nested_html
            elif mime_type == "text/plain" and part.get("body", {}).get("data"):
                plain_text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
            elif mime_type == "text/html" and part.get("body", {}).get("data"):
                html_text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
        
        return plain_text, html_text

    if "body" in payload_data and payload_data["body"].get("data"):
        return base64.urlsafe_b64decode(payload_data["body"]["data"]).decode("utf-8", errors="ignore")
    elif "parts" in payload_data:
        plain, html = extract_from_parts(payload_data["parts"])
        if plain:
            return plain
        elif html:
            import re
            text = re.sub(r'<[^>]+>', '', html)
            return re.sub(r'\s+', ' ', text).strip()
    
    return "[No text content]"


# ==================== Email API Routes ====================

@app.get("/emails")
def get_emails(
    current_user: dict = Depends(get_current_user),
    limit: int = 50,
    offset: int = 0
):
    """Get emails for the authenticated user."""
    db = SessionLocal()
    try:
        emails = db.query(Email).filter(
            Email.user_id == current_user["user_id"]
        ).order_by(Email.received_at.desc()).offset(offset).limit(limit).all()
        
        return [e.to_dict() for e in emails]
    finally:
        db.close()


# ==================== Send Email Routes ====================

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from fastapi import UploadFile, File, Form
import mimetypes


class EmailRequest(BaseModel):
    to: str
    subject: str
    body: str


@app.post("/send-email")
def send_email(
    email_req: EmailRequest,
    current_user: dict = Depends(get_current_user)
):
    """Send an email using Gmail API."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = Credentials(
            token=user.token,
            refresh_token=user.refresh_token,
            token_uri=user.token_uri or "https://oauth2.googleapis.com/token",
            client_id=user.client_id or os.getenv("CLIENT_ID"),
            client_secret=user.client_secret or os.getenv("CLIENT_SECRET"),
            scopes=user.scopes or SCOPES
        )

        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            user.token = creds.token
            user.expiry = creds.expiry
            db.commit()

        service = build("gmail", "v1", credentials=creds)

        message = MIMEMultipart()
        message["to"] = email_req.to
        message["from"] = user.email_id
        message["subject"] = email_req.subject
        message.attach(MIMEText(email_req.body, "plain"))

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        sent_message = service.users().messages().send(
            userId="me",
            body={"raw": raw_message}
        ).execute()

        print(f"📤 Email sent to {email_req.to}: {email_req.subject}")

        return {
            "status": "success",
            "message_id": sent_message["id"],
            "thread_id": sent_message["threadId"]
        }

    except Exception as e:
        print(f"❌ Error sending email: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)