To create the table, run:
python
from models import Base, engine
Base.metadata.create_all(bind=engine)
