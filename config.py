from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = ""
    # Каталог данных: БД + .session файлы пользователей
    data_dir: str = "data"
    # Ключ шифрования api_hash в БД (если пусто — используется bot_token)
    encryption_key: str = ""
    allowed_user_ids: str = ""
    gift_send_delay: float = 2.0

    def allowed_ids(self) -> set[int]:
        if not self.allowed_user_ids.strip():
            return set()
        return {int(x.strip()) for x in self.allowed_user_ids.split(",") if x.strip()}


def get_settings() -> Settings:
    return Settings()
