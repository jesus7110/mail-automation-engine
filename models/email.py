from sqlalchemy import Column, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class Email(Base):
    """
    Email storage table for per-user email monitoring.
    Stores emails received via Gmail Pub/Sub webhook.
    """
    __tablename__ = "emails"

    id = Column(String(50), primary_key=True)  # Gmail message ID
    user_id = Column(String(10), ForeignKey("users.user_id"), nullable=False, index=True)
    from_address = Column(String(500), nullable=True)
    subject = Column(String(1000), nullable=True)
    body = Column(Text, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Email(id='{self.id}', user_id='{self.user_id}', subject='{self.subject[:30] if self.subject else ''}'...)>"

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "from": self.from_address,
            "subject": self.subject,
            "body": self.body,
            "received_at": self.received_at.isoformat() if self.received_at else None
        }
