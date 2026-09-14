"""
b2_service.py - Servicio Backblaze B2 para subida y descarga de backups.
Credenciales fijas para el proyecto de backup de Neon.tech.
"""

import os
from b2sdk.v2 import B2Api, InMemoryAccountInfo
from datetime import datetime

# ===========================
# CREDENCIALES BACKBLAZE B2
# ===========================
B2_KEY_ID = "005cce85e2cd0b10000000001"
B2_APP_KEY = "K005oqatMmQJJxMXwiBaSz+H6JUJ9E4"
B2_BUCKET_NAME = "mi-app-backups-2026"

# Sesión global reutilizable
_b2_api = None
_b2_bucket = None


def _get_b2_session():
    """Obtener (o crear) la sesión B2 y el bucket."""
    global _b2_api, _b2_bucket
    try:
        if _b2_api is None:
            info = InMemoryAccountInfo()
            _b2_api = B2Api(info)
            _b2_api.authorize_account("production", B2_KEY_ID, B2_APP_KEY)
        # Re-obtener el bucket (es barato y evita sesiones caducadas)
        _b2_bucket = _b2_api.get_bucket_by_name(B2_BUCKET_NAME)
        return _b2_bucket
    except Exception as e:
        # Reiniciar si falla la sesión
        _b2_api = None
        _b2_bucket = None
        raise Exception(f"Error al conectar con Backblaze B2: {e}")


def upload_file_to_b2(local_path: str, remote_filename: str = None) -> dict:
    """
    Sube un archivo local al bucket de Backblaze B2.

    Args:
        local_path: Ruta local del archivo a subir (ej. D:\\BACKUP\\neondb_backup.sql)
        remote_filename: Nombre del archivo en B2. Si None, usa el nombre del archivo local.

    Returns:
        dict con información del archivo subido: {file_name, file_id, size, upload_time}
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"El archivo local no existe: {local_path}")

    if remote_filename is None:
        remote_filename = os.path.basename(local_path)

    bucket = _get_b2_session()
    file_info = bucket.upload_local_file(
        local_file=local_path,
        file_name=remote_filename,
    )

    return {
        "file_name": file_info.file_name,
        "file_id": file_info.id_,
        "size": file_info.size,
        "upload_time": datetime.now().isoformat(),
        "bucket": B2_BUCKET_NAME,
    }


def download_file_from_b2(remote_filename: str, target_path: str) -> dict:
    """
    Descarga un archivo desde Backblaze B2 a una ruta local.

    Args:
        remote_filename: Nombre del archivo en B2 (ej. "neondb_backup.sql")
        target_path: Ruta local donde guardar el archivo descargado.

    Returns:
        dict con información de la descarga: {file_name, size, download_time}
    """
    bucket = _get_b2_session()

    # Descargar el archivo
    downloaded = bucket.download_file_by_name(remote_filename)
    downloaded.save_to(target_path)

    size = os.path.getsize(target_path) if os.path.exists(target_path) else 0
    return {
        "file_name": remote_filename,
        "target_path": target_path,
        "size": size,
        "download_time": datetime.now().isoformat(),
    }


def get_b2_file_info(remote_filename: str) -> dict | None:
    """
    Obtiene información del último archivo subido a B2.

    Args:
        remote_filename: Nombre del archivo en B2.

    Returns:
        dict con info del archivo, o None si no existe.
    """
    try:
        bucket = _get_b2_session()
        for file_version, _ in bucket.ls(latest_only=True):
            if file_version.file_name == remote_filename:
                return {
                    "file_name": file_version.file_name,
                    "file_id": file_version.id_,
                    "size": file_version.size,
                    "upload_timestamp": file_version.upload_timestamp,
                    "upload_time": datetime.fromtimestamp(
                        file_version.upload_timestamp / 1000
                    ).isoformat() if file_version.upload_timestamp else None,
                    "bucket": B2_BUCKET_NAME,
                }
        return None
    except Exception:
        return None


def get_b2_status() -> dict:
    """
    Retorna el estado de conexión con Backblaze B2 y resumen del bucket.

    Returns:
        dict con {connected, bucket, files_count, last_file, last_upload_time, error}
    """
    try:
        bucket = _get_b2_session()
        files = list(bucket.ls(latest_only=True))
        file_list = []
        for f, _ in files:
            file_list.append({
                "file_name": f.file_name,
                "size": f.size,
                "upload_time": datetime.fromtimestamp(
                    f.upload_timestamp / 1000
                ).isoformat() if f.upload_timestamp else None,
            })

        last_file = file_list[0] if file_list else None
        return {
            "connected": True,
            "bucket": B2_BUCKET_NAME,
            "files_count": len(file_list),
            "files": file_list,
            "last_file": last_file,
            "last_upload_time": last_file["upload_time"] if last_file else None,
            "error": None,
        }
    except Exception as e:
        return {
            "connected": False,
            "bucket": B2_BUCKET_NAME,
            "files_count": 0,
            "files": [],
            "last_file": None,
            "last_upload_time": None,
            "error": str(e),
        }
