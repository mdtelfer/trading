# scripts/show_env.py
from src.config import ENV_PATH, ROOT, env

print("ROOT:", ROOT)
print(".env path:", ENV_PATH, "exists:", ENV_PATH.exists())
print("FRED_API_KEY:", env("FRED_API_KEY"))
print("DB_HOST:", env("DB_HOST"))
