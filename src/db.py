from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import env

DB_HOST = env("DB_HOST", "localhost")
DB_PORT = env("DB_PORT", "5432")
DB_NAME = env("DB_NAME", "trading")
DB_USER = env("DB_USER", "postgres")
DB_PASS = env("DB_PASSWORD", "postgres")

URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
