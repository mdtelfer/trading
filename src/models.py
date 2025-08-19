from sqlalchemy import Column, String, Float, TIMESTAMP, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from .db import Base

class FundamentalTick(Base):
    __tablename__ = "fundamental_ticks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    feature = Column(String, index=True)
    symbol = Column(String, index=True)
    bid = Column(Float)
    ask = Column(Float)
    time = Column(TIMESTAMP, index=True)
    raw = Column(JSONB)   # guardar tick completo si quieres
