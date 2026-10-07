from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import ConfigDict, model_validator
from typing import List
from dotenv import load_dotenv
from loguru import logger
import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def get_project_root() -> Path:
    """Находит корень проекта"""
    import sys

    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists() or (parent / ".env").exists():
            return parent.absolute()
    return current.parent


def get_version() -> str:
    import sys

    if getattr(sys, 'frozen', False):
        try:
            from app._version import __version__
            return __version__
        except ImportError:
            pass

    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys._MEIPASS) / "pyproject.toml")
    candidates.append(get_project_root() / "pyproject.toml")

    for candidate in candidates:
        try:
            import tomllib
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            version = data.get("project", {}).get("version", "")
            if version:
                return version
        except Exception:
            continue

    try:
        from app._version import __version__
        return __version__
    except ImportError:
        pass

    return "unknown"


def generate_key_from_password(password: str, salt: bytes = None) -> tuple[bytes, bytes]:
    if salt is None:
        salt = os.urandom(16)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
    return key, salt


def encrypt_password(password: str, password_protect: str = "protect_me_1c_service") -> tuple[str, str]:
    salt = os.urandom(16)
    key, _ = generate_key_from_password(password_protect, salt)
    f = Fernet(key)
    encrypted = f.encrypt(password.encode())
    encrypted_b64 = base64.urlsafe_b64encode(encrypted).decode()
    salt_b64 = base64.urlsafe_b64encode(salt).decode()
    return encrypted_b64, salt_b64


def decrypt_password(encrypted_pwd: str, salt_b64: str, password_protect: str = "protect_me_1c_service") -> str:
    salt = base64.urlsafe_b64decode(salt_b64)
    key, _ = generate_key_from_password(password_protect, salt)
    f = Fernet(key)
    encrypted = base64.urlsafe_b64decode(encrypted_pwd)
    return f.decrypt(encrypted).decode()


def secure_db_password(dotenv_path: Path) -> None:
    """Если DB_PWD открыт - шифрует его"""
    if not dotenv_path.exists():
        return
    
    protect_password = "protect_me_1c_service"
    
    lines = dotenv_path.read_text(encoding='utf-8').splitlines()
    env_content = {}
    
    for line in lines:
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            env_content[key.strip()] = value.strip()
    
    if 'DB_PWD' in env_content:
        current_pwd = env_content['DB_PWD']
        # Если пароль короткий (открытый) - шифруем
        if len(current_pwd) < 100:  
            logger.warning("🔐 Обнаружен открытый DB_PWD! Шифруем...")
            encrypted_pwd, salt_b64 = encrypt_password(current_pwd, protect_password)
            
            # Заменяем в .env: DB_PWD=encrypted|salt
            new_lines = []
            for line in lines:
                if line.strip().startswith('DB_PWD='):
                    new_lines.append(f"DB_PWD={encrypted_pwd}|{salt_b64}")
                else:
                    new_lines.append(line)
            
            dotenv_path.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
            logger.info(f"✅ DB_PWD зашифрован в .env")


# Инициализация
project_root = get_project_root()
dotenv_path = project_root / ".env"
secure_db_password(dotenv_path)
load_dotenv(dotenv_path=dotenv_path)


class Config(BaseSettings):
    APP_PATH: str = str(project_root)
    REPORT_DIR: str = r'//16x-1cfs01.one.local/1CExchange$/DWH_DATALENS'
    ALLOWED_EXTENSIONS: List[str] = [".txt", ".csv", ".xlsx", ".pdf"]
    
    DB_HOST: str = "16X-DL-MASTER01.one.local"
    DB_PORT: int = 5432
    DB_NAME: str = "dbt"
    DB_USER: str = "1c-service01"
    DB_PWD: str = ""  
    
    UV_HOST: str = "0.0.0.0"
    UV_PORT: int = 8000
    LOG_LEVEL: str = "info"
    LOG_FILE_LEVEL: str = "info"
    LOG_FILE_DIR: str = ''

    model_config = ConfigDict(
        env_file=dotenv_path,
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore"
    )

    @model_validator(mode='after')
    def decrypt_db_password(self):
        """DB_PWD в .env зашифрован → config.DB_PWD расшифрован"""
        protect_password = "protect_me_1c_service"
        
        if '|' in self.DB_PWD and len(self.DB_PWD) > 100:
            try:
                encrypted_pwd, salt_b64 = self.DB_PWD.split('|', 1)
                decrypted = decrypt_password(encrypted_pwd, salt_b64, protect_password)
                self.DB_PWD = decrypted  # ✅ Расшифрованный пароль!
                logger.debug("✅ DB_PWD расшифрован")
            except Exception as e:
                logger.error(f"❌ Ошибка расшифровки: {e}")
                self.DB_PWD = ""
        elif self.DB_PWD:
            logger.warning("⚠️ DB_PWD в открытом виде - будет зашифрован при следующем запуске")
        
        return self

    @property
    def db_password(self) -> str:
        return self.DB_PWD


config = Config()
logger.info(f"✅ Конфиг: {config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
logger.info(f"DB_PWD: {'готов' if config.db_password else 'отсутствует'}")
