from sqlalchemy import Column, String, DateTime, Boolean, ARRAY
from sqlalchemy.orm import Session
from .database import Base


class User(Base):
    __tablename__ = "users"

    # Primary key - auto-generated in format UID-XXXX
    user_id = Column(String(10), primary_key=True)
    
    # User profile information
    email_id = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)
    given_name = Column(String(255), nullable=True)
    family_name = Column(String(255), nullable=True)
    profile_pic_url = Column(String(500), nullable=True)
    
    # OAuth token information
    token = Column(String(2000), nullable=True)
    refresh_token = Column(String(500), nullable=True)
    token_uri = Column(String(500), nullable=True)
    client_id = Column(String(255), nullable=True)
    client_secret = Column(String(255), nullable=True)
    scopes = Column(ARRAY(String), nullable=True)
    universe_domain = Column(String(255), nullable=True)
    
    # Additional fields
    account = Column(String(255), nullable=True)
    expiry = Column(DateTime, nullable=True)
    history_id = Column(String(100), nullable=True)
    
    # Monitoring status
    monitoring_status = Column(Boolean, default=False, nullable=False)
    last_watch_expiry = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<User(user_id='{self.user_id}', email_id='{self.email_id}')>"

    def to_dict(self):
        """Convert to dictionary for API responses (excludes sensitive tokens)."""
        return {
            "user_id": self.user_id,
            "email_id": self.email_id,
            "name": self.name,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "profile_pic_url": self.profile_pic_url,
            "monitoring_status": self.monitoring_status
        }


def get_next_user_id(session: Session) -> str:
    """
    Generate the next user ID in sequence using database counter.
    Thread-safe and persistent across restarts.
    """
    from .counter import get_next_counter_value
    next_num = get_next_counter_value(session, "user_id")
    return f"UID-{next_num:04d}"

