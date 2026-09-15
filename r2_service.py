"""
r2_service.py - Servicio Cloudflare R2 (S3-compatible) para subida y descarga de backups.
Usa boto3 con el endpoint de R2. Credenciales leídas desde .env o variables de entorno.
"""

import os
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

# Cargar .env desde el directorio del propio archivo
_BASE_DIR = Path(__file__).resolve().parent
load_dotenv(_BASE_DIR / ".env")

# ===========================
# CREDENCIALES CLOUDFLARE R2
# ===========================
R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID",     "88d2c340e2b431380f1d401f9d9b68e2")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "dbdcf61a57b8703f9d835412d266ae09dfd85744b1c3277c3081b59331176ac7")
R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME",       "backups-db")
R2_ENDPOINT_URL      = os.getenv("R2_ENDPOINT_URL",      "https://4288972a799d4e0b15cc80ace51e84e4.r2.cloudflarestorage.com")

# Nombre fijo del objeto en R2 — se sobreescribe en cada backup
FIXED_REMOTE_NAME = "backup.sql"

# Cliente global reutilizable
_s3_client = None


def _get_r2_client():
    """Obtener (o crear) el cliente S3 apuntando a R2."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            service_name="s3",
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(
                s3={"addressing_style": "path"},
                signature_version="s3v4",
            ),
        )
    return _s3_client


def upload_file_to_b2(local_path: str, remote_filename: str = None) -> dict:
    """
    Sube un archivo local al bucket de Cloudflare R2.

    Siempre usa el nombre fijo FIXED_REMOTE_NAME ('backup.sql') para
    sobreescribir el objeto anterior y mantener un único archivo actualizado.

    Args:
        local_path: Ruta local del archivo a subir.
        remote_filename: Ignorado; se usa siempre FIXED_REMOTE_NAME.

    Returns:
        dict con {file_name, file_id, size, upload_time, bucket}
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"El archivo local no existe: {local_path}")

    key = FIXED_REMOTE_NAME  # nombre fijo en R2
    client = _get_r2_client()

    client.upload_file(
        Filename=local_path,
        Bucket=R2_BUCKET_NAME,
        Key=key,
    )

    file_size = os.path.getsize(local_path)
    now_iso = datetime.now().isoformat()

    return {
        "file_name": key,
        "file_id": f"r2://{R2_BUCKET_NAME}/{key}",
        "size": file_size,
        "upload_time": now_iso,
        "bucket": R2_BUCKET_NAME,
    }


def download_file_from_b2(remote_filename: str, target_path: str) -> dict:
    """
    Descarga el backup desde Cloudflare R2 a una ruta local.

    Siempre descarga el objeto con nombre fijo FIXED_REMOTE_NAME.

    Args:
        remote_filename: Ignorado; se usa siempre FIXED_REMOTE_NAME.
        target_path: Ruta local donde guardar el archivo.

    Returns:
        dict con {file_name, target_path, size, download_time}
    """
    key = FIXED_REMOTE_NAME
    client = _get_r2_client()

    try:
        client.download_file(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            Filename=target_path,
        )
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("NoSuchKey", "404"):
            raise FileNotFoundError(
                f"No se encontró '{key}' en R2 (bucket: {R2_BUCKET_NAME})"
            ) from e
        raise

    size = os.path.getsize(target_path) if os.path.exists(target_path) else 0
    return {
        "file_name": key,
        "target_path": target_path,
        "size": size,
        "download_time": datetime.now().isoformat(),
    }


def get_b2_file_info(remote_filename: str = None) -> dict | None:
    """
    Obtiene información del objeto fijo en R2.

    Args:
        remote_filename: Ignorado; siempre consulta FIXED_REMOTE_NAME.

    Returns:
        dict con info del archivo, o None si no existe.
    """
    key = FIXED_REMOTE_NAME
    try:
        client = _get_r2_client()
        resp = client.head_object(Bucket=R2_BUCKET_NAME, Key=key)
        last_modified = resp.get("LastModified")
        return {
            "file_name": key,
            "file_id": f"r2://{R2_BUCKET_NAME}/{key}",
            "size": resp.get("ContentLength", 0),
            "upload_time": last_modified.isoformat() if last_modified else None,
            "bucket": R2_BUCKET_NAME,
        }
    except ClientError:
        return None
    except Exception:
        return None


def get_b2_status() -> dict:
    """
    Retorna el estado de conexión con Cloudflare R2 y el objeto de backup.

    Returns:
        dict con {connected, bucket, files_count, files, last_file,
                  last_upload_time, error}
    """
    try:
        client = _get_r2_client()
        response = client.list_objects_v2(Bucket=R2_BUCKET_NAME)
        contents = response.get("Contents", [])

        file_list = []
        for obj in contents:
            modified = obj.get("LastModified")
            file_list.append({
                "file_name": obj["Key"],
                "size": obj.get("Size", 0),
                "upload_time": modified.isoformat() if modified else None,
            })

        last_file = file_list[0] if file_list else None
        return {
            "connected": True,
            "bucket": R2_BUCKET_NAME,
            "files_count": len(file_list),
            "files": file_list,
            "last_file": last_file,
            "last_upload_time": last_file["upload_time"] if last_file else None,
            "error": None,
        }
    except Exception as e:
        return {
            "connected": False,
            "bucket": R2_BUCKET_NAME,
            "files_count": 0,
            "files": [],
            "last_file": None,
            "last_upload_time": None,
            "error": str(e),
        }
