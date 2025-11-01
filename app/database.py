# app/database.py
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from . import config

engine = create_engine(config.DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    # 在这里导入所有模型，以便 Base 能找到它们
    from . import models
    print("Initializing database and creating tables if they don't exist...")
    Base.metadata.create_all(bind=engine)
    print("Database initialization complete.")