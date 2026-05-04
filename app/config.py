from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    collect_hour: int = 3
    collect_minute: int = 0

    # Polar OAuth
    polar_client_id: str = ""
    polar_client_secret: str = ""
    polar_redirect_uri: str = "https://web-production-3668.up.railway.app/auth/polar/callback"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()