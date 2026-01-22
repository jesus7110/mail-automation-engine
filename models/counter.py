from sqlalchemy import Column, String, Integer
from .database import Base


class Counter(Base):
    """
    Global counters table for managing sequential IDs.
    Used for generating user IDs (UID-XXXX format) and other sequences.
    """
    __tablename__ = "counters"

    name = Column(String(50), primary_key=True)  # Counter name, e.g., "user_id"
    value = Column(Integer, default=0, nullable=False)

    def __repr__(self):
        return f"<Counter(name='{self.name}', value={self.value})>"


def get_next_counter_value(session, counter_name: str) -> int:
    """
    Get the next value for a counter, incrementing it atomically.
    Creates the counter if it doesn't exist.
    
    Args:
        session: SQLAlchemy session
        counter_name: Name of the counter (e.g., "user_id")
    
    Returns:
        The next integer value
    """
    counter = session.query(Counter).filter_by(name=counter_name).with_for_update().first()
    
    if counter is None:
        counter = Counter(name=counter_name, value=1)
        session.add(counter)
        session.commit()
        return 1
    
    counter.value += 1
    session.commit()
    return counter.value
