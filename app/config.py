from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Personal Task Assistant"
    app_version: str = "0.8.0"
    public_base_url: str = ""
    task_store: str = "sqlite"
    database_url: str = "sqlite:///./task_tracker.db"
    task_tracker_api_key: str = "change-me"
    session_secret_key: str = "change-this-long-random-string"
    session_cookie_https: bool = False
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    allowed_google_emails: str = ""
    task_history_sheet_id: str = ""
    task_history_sheet_tab: str = "Task History"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def is_hosted(self) -> bool:
        return self.use_firestore or self.session_cookie_https or self.public_base_url.startswith("https://")

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)

    @property
    def local_dev_auth_enabled(self) -> bool:
        return not self.is_hosted and not self.google_oauth_enabled

    @property
    def normalized_public_base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def use_firestore(self) -> bool:
        return self.task_store.lower() == "firestore"

    @property
    def task_history_enabled(self) -> bool:
        return bool(self.task_history_sheet_id.strip())

    @property
    def allowed_email_set(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.allowed_google_emails.split(",")
            if email.strip()
        }

    @property
    def local_dev_user_email(self) -> str:
        emails = [
            email.strip().lower()
            for email in self.allowed_google_emails.split(",")
            if email.strip()
        ]
        return emails[0] if emails else "local-dev@task-tracker.local"

    def validate_runtime_config(self) -> None:
        errors = []
        if self.is_hosted:
            if self.task_tracker_api_key in {"", "change-me"}:
                errors.append("TASK_TRACKER_API_KEY must be set to a non-default value")
            if self.session_secret_key in {"", "change-this-long-random-string"}:
                errors.append("SESSION_SECRET_KEY must be set to a non-default value")
            if len(self.session_secret_key) < 32:
                errors.append("SESSION_SECRET_KEY must be at least 32 characters")
        if self.google_oauth_enabled and not self.allowed_email_set:
            errors.append("ALLOWED_GOOGLE_EMAILS must be non-empty when Google OAuth is enabled")
        if errors:
            raise RuntimeError("Invalid runtime configuration: " + "; ".join(errors))


@lru_cache
def get_settings() -> Settings:
    return Settings()
