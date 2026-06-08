from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    collect_hour: int = 3
    collect_minute: int = 0

    # Polar OAuth
    polar_client_id: str = ""
    polar_client_secret: str = ""
    polar_redirect_uri: str = ""

    # Withings OAuth
    withings_client_id: str = ""
    withings_client_secret: str = ""
    withings_redirect_uri: str = ""

    graphhopper_api_key: str
    garmin_encryption_key: str = ""

    # ML — chemin absolu vers cronos-ml/ (optionnel, auto-détecté sinon)
    cronos_ml_dir: str = ""

    # Email notifications
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "CRONOS Peakflow <noreply@peakflow.ai>"

    # Heure UTC d'envoi des notifications (0-23)
    notif_hour: int = 7

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()