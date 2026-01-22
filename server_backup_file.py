import os, json, base64
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

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

TOKEN_DIR = "tokens"
EMAIL_STORE = "data/emails.json"
HISTORY_STORE = "data/history.json"  # Store last known history IDs
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "mail-automation-engine")

os.makedirs(TOKEN_DIR, exist_ok=True)
os.makedirs("data", exist_ok=True)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ---------------- UI ----------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ---------------- OAuth ----------------

@app.get("/auth/google")
def auth_google():
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
    redirect_uri = os.getenv("REDIRECT_URI", "http://localhost:8000/auth/google/callback")
    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    flow.fetch_token(code=code)
    creds = flow.credentials

    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    email = profile["emailAddress"]
    history_id = profile["historyId"]  # Get current history ID

    with open(f"{TOKEN_DIR}/{email}.json", "w") as f:
        f.write(creds.to_json())

    # Store initial history ID
    history_data = {}
    if os.path.exists(HISTORY_STORE):
        history_data = json.load(open(HISTORY_STORE))
    history_data[email] = history_id
    json.dump(history_data, open(HISTORY_STORE, "w"), indent=2)
    print(f"📝 Stored initial history ID: {history_id} for {email}")

    # Start Gmail watch
    service.users().watch(
        userId="me",
        body={
            "labelIds": ["INBOX"],
            "topicName": f"projects/{GCP_PROJECT_ID}/topics/gmail-inbox-events"
        }
    ).execute()

    return {"status": "connected", "email": email, "history_id": history_id}

# ---------------- Gmail Push Webhook ----------------

@app.post("/webhooks/gmail")
async def gmail_push(request: Request):
    body = await request.json()

    data = body["message"]["data"]
    payload = json.loads(base64.b64decode(data).decode())
    print("🔥 WEBHOOK HIT 🔥")
    print(f"Payload: {payload}")

    email = payload["emailAddress"]
    new_history_id = payload["historyId"]

    # Load stored history IDs
    history_data = {}
    if os.path.exists(HISTORY_STORE):
        history_data = json.load(open(HISTORY_STORE))
    
    # Get the last known history ID for this user
    last_history_id = history_data.get(email)
    
    if not last_history_id:
        print(f"⚠️ No stored history ID for {email}. Storing current and waiting for next event.")
        history_data[email] = new_history_id
        json.dump(history_data, open(HISTORY_STORE, "w"), indent=2)
        return {"status": "ok", "message": "Stored initial history ID"}

    print(f"📧 Querying history from {last_history_id} (webhook gave {new_history_id})")

    try:
        creds = Credentials.from_authorized_user_file(
            f"{TOKEN_DIR}/{email}.json", SCOPES
        )

        if creds.expired:
            creds.refresh(GoogleRequest())

        service = build("gmail", "v1", credentials=creds)

        history = service.users().history().list(
            userId="me",
            startHistoryId=last_history_id,
            historyTypes=["messageAdded"]
        ).execute()

        print(f"📬 History response: {history}")

        # Get the actual current history ID from the API response (most reliable)
        api_history_id = int(history.get("historyId", new_history_id))
        
        # Always use the highest history ID we've seen to prevent loops from queued webhooks
        current_stored = int(last_history_id)
        latest_history_id = max(api_history_id, int(new_history_id), current_stored)
        
        # Update history ID immediately to prevent duplicate processing from concurrent webhooks
        history_data[email] = str(latest_history_id)
        json.dump(history_data, open(HISTORY_STORE, "w"), indent=2)
        print(f"📝 Updated history ID to: {latest_history_id}")

        stored = []
        if os.path.exists(EMAIL_STORE):
            stored = json.load(open(EMAIL_STORE))

        existing_ids = {e["id"] for e in stored}

        for h in history.get("history", []):
            for m in h.get("messagesAdded", []):
                msg_id = m["message"]["id"]
                if msg_id in existing_ids:
                    continue  # Skip duplicates
                
                try:
                    msg = service.users().messages().get(
                        userId="me",
                        id=msg_id,
                        format="full"
                    ).execute()
                except Exception as fetch_error:
                    # Message may have been deleted, moved to trash, or is otherwise unavailable
                    print(f"⚠️ Skipping message {msg_id}: {fetch_error}")
                    continue

                headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                
                # Extract body - helper function for recursive part extraction
                def extract_body_from_parts(parts, preferred_type="text/plain"):
                    """Recursively extract body from email parts"""
                    plain_text = ""
                    html_text = ""
                    calendar_text = ""
                    
                    for part in parts:
                        mime_type = part.get("mimeType", "")
                        
                        # Handle nested multipart
                        if mime_type.startswith("multipart/") and "parts" in part:
                            nested_plain, nested_html, nested_cal = extract_body_from_parts(part["parts"])
                            if nested_plain:
                                plain_text = nested_plain
                            if nested_html:
                                html_text = nested_html
                            if nested_cal:
                                calendar_text = nested_cal
                        
                        # Extract plain text
                        elif mime_type == "text/plain" and part.get("body", {}).get("data"):
                            plain_text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                        
                        # Extract HTML as fallback
                        elif mime_type == "text/html" and part.get("body", {}).get("data"):
                            html_text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                        
                        # Extract calendar invite description
                        elif mime_type == "text/calendar" and part.get("body", {}).get("data"):
                            calendar_data = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                            # Extract DESCRIPTION from ics file
                            for line in calendar_data.split("\n"):
                                if line.startswith("DESCRIPTION:"):
                                    calendar_text = line.replace("DESCRIPTION:", "").strip()
                                    break
                    
                    return plain_text, html_text, calendar_text
                
                # Extract body
                body = ""
                payload_data = msg["payload"]
                
                if "body" in payload_data and payload_data["body"].get("data"):
                    # Simple message with direct body
                    body = base64.urlsafe_b64decode(payload_data["body"]["data"]).decode("utf-8", errors="ignore")
                elif "parts" in payload_data:
                    # Multipart message
                    plain, html, calendar = extract_body_from_parts(payload_data["parts"])
                    
                    # Priority: plain text > html (stripped) > calendar description
                    if plain:
                        body = plain
                    elif html:
                        # Strip HTML tags for basic text extraction
                        import re
                        body = re.sub(r'<[^>]+>', '', html)
                        body = re.sub(r'\s+', ' ', body).strip()
                    elif calendar:
                        body = f"[Calendar Invite] {calendar}"
                    else:
                        body = "[No text content - may contain attachments only]"


                stored.append({
                    "from": headers.get("From"),
                    "subject": headers.get("Subject"),
                    "body": body,
                    "id": msg["id"]
                })
                print(f"✅ Stored: {headers.get('Subject')}")

        json.dump(stored, open(EMAIL_STORE, "w"), indent=2)
        
        return {"status": "ok", "emails_added": len(history.get("history", [])), "history_id": latest_history_id}
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"status": "error", "message": str(e)}

# ---------------- View Stored Emails ----------------

@app.get("/emails")
def get_emails():
    if not os.path.exists(EMAIL_STORE):
        return []
    return json.load(open(EMAIL_STORE))

# ---------------- Send Email ----------------

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pydantic import BaseModel
from typing import Optional, List
from fastapi import UploadFile, File, Form
import mimetypes

class EmailRequest(BaseModel):
    to: str
    subject: str
    body: str
    sender_email: str  # The authenticated Gmail account to send from

@app.post("/send-email")
def send_email(email_req: EmailRequest):
    """
    Send an email using Gmail API (JSON body, no attachments).
    
    Request body:
    - to: Recipient email address
    - subject: Email subject
    - body: Email body (plain text)
    - sender_email: The authenticated Gmail account to send from
    """
    token_file = f"{TOKEN_DIR}/{email_req.sender_email}.json"
    
    if not os.path.exists(token_file):
        return {"status": "error", "message": f"No credentials found for {email_req.sender_email}. Please authenticate first at /auth/google"}
    
    try:
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        
        if creds.expired:
            creds.refresh(GoogleRequest())
            with open(token_file, "w") as f:
                f.write(creds.to_json())
        
        service = build("gmail", "v1", credentials=creds)
        
        message = MIMEMultipart()
        message["to"] = email_req.to
        message["from"] = email_req.sender_email
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
        return {"status": "error", "message": str(e)}


@app.post("/send-email-with-attachments")
async def send_email_with_attachments(
    to: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    sender_email: str = Form(...),
    attachments: List[UploadFile] = File(default=[])
):
    """
    Send an email with attachments using Gmail API.
    
    Form fields:
    - to: Recipient email address
    - subject: Email subject
    - body: Email body (plain text)
    - sender_email: The authenticated Gmail account to send from
    - attachments: File(s) to attach (optional, can upload multiple)
    """
    token_file = f"{TOKEN_DIR}/{sender_email}.json"
    
    if not os.path.exists(token_file):
        return {"status": "error", "message": f"No credentials found for {sender_email}. Please authenticate first at /auth/google"}
    
    try:
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        
        if creds.expired:
            creds.refresh(GoogleRequest())
            with open(token_file, "w") as f:
                f.write(creds.to_json())
        
        service = build("gmail", "v1", credentials=creds)
        
        # Create the email message
        message = MIMEMultipart()
        message["to"] = to
        message["from"] = sender_email
        message["subject"] = subject
        message.attach(MIMEText(body, "plain"))
        
        # Add attachments
        for file in attachments:
            content = await file.read()
            
            # Get MIME type
            mime_type, _ = mimetypes.guess_type(file.filename)
            if mime_type is None:
                mime_type = "application/octet-stream"
            
            main_type, sub_type = mime_type.split("/", 1)
            
            # Create attachment
            attachment = MIMEBase(main_type, sub_type)
            attachment.set_payload(content)
            encoders.encode_base64(attachment)
            attachment.add_header(
                "Content-Disposition",
                f"attachment; filename={file.filename}"
            )
            message.attach(attachment)
        
        # Encode and send
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        
        sent_message = service.users().messages().send(
            userId="me",
            body={"raw": raw_message}
        ).execute()
        
        attachment_names = [f.filename for f in attachments]
        print(f"📤 Email sent to {to}: {subject} (attachments: {attachment_names})")
        
        return {
            "status": "success",
            "message_id": sent_message["id"],
            "thread_id": sent_message["threadId"],
            "attachments": attachment_names
        }
        
    except Exception as e:
        print(f"❌ Error sending email: {e}")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
    