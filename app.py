from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
from psycopg2 import sql
from apscheduler.schedulers.background import BackgroundScheduler
import subprocess
import os
import re
import shutil
from pathlib import Path
from datetime import datetime
import json
import threading
import r2_service as b2_service  # Cloudflare R2 (S3-compatible) – alias para compatibilidad

app = Flask(__name__)
CORS(app)

# ===========================
# CONFIGURACION
# ===========================

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR = Path(r"D:\BACKUP")  # Tus backups están en D:\BACKUP
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULT_DATABASE = "tienda_final"
DEFAULT_BACKUP_FILENAME = "tienda_db_backup"

BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    """Cargar configuracion desde archivo JSON."""
    default_config = {
        "backup_interval_minutes": 1,
        "last_backup_time": None,
        "backup_size": 0,
        "backup_filename": DEFAULT_BACKUP_FILENAME,
        "backup_database": None,
        "current_database": DEFAULT_DATABASE,
    }

    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                config = json.load(f)
            return {**default_config, **config}
        except Exception as e:
            print(f"Error leyendo configuracion: {e}")

    return default_config


def save_config(config):
    """Guardar configuracion en archivo JSON."""
    try:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        return True
    except Exception as e:
        print(f"Error guardando configuracion: {e}")
        return False


def load_database_name():
    """Cargar el nombre de la base de datos actual."""
    return load_config().get("current_database") or DEFAULT_DATABASE


def save_database_name(database_name):
    """Guardar el nombre de la base de datos actual."""
    config = load_config()
    config["current_database"] = database_name
    save_config(config)


# ===========================
# CREDENCIALES NEON.TECH (CLOUD)
# ===========================
NEON_HOST = "ep-delicate-term-b4zszln3-pooler.c-6.us-east-2.aws.neon.tech"
NEON_DATABASE = "neondb"
NEON_USER = "neondb_owner"
NEON_PASSWORD = "npg_je9vgbuEIcJ5"
NEON_PORT = 5432
NEON_SSLMODE = "require"

DB_CONFIG = {
    "host": NEON_HOST,
    "database": NEON_DATABASE,
    "user": NEON_USER,
    "password": NEON_PASSWORD,
    "port": NEON_PORT,
    "sslmode": NEON_SSLMODE,
}

scheduler = BackgroundScheduler()
scheduler_running = False
CURRENT_BACKUP_FILENAME = load_config().get("backup_filename", DEFAULT_BACKUP_FILENAME)


# ===========================
# UTILIDADES
# ===========================

def normalize_backup_filename(filename=None):
    """Convertir la entrada del usuario en un nombre seguro de archivo .sql."""
    raw_name = str(filename or "").strip()
    if not raw_name:
        raw_name = DEFAULT_BACKUP_FILENAME

    raw_name = raw_name.replace("\\", "/").split("/")[-1]
    if raw_name.lower().endswith(".sql"):
        raw_name = raw_name[:-4]

    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_name).strip("_")
    return safe_name or DEFAULT_BACKUP_FILENAME


def get_backup_path(filename=None):
    backup_filename = normalize_backup_filename(filename)
    return BACKUP_DIR / f"{backup_filename}.sql"


def find_postgres_tool(tool_name):
    """Buscar pg_dump.exe o psql.exe sin depender de una sola version."""
    env_var = f"{tool_name.upper()}_PATH"
    env_path = os.environ.get(env_var)
    if env_path and Path(env_path).exists():
        return env_path

    for executable in (tool_name, f"{tool_name}.exe"):
        found = shutil.which(executable)
        if found:
            return found

    postgres_root = Path(r"C:\Program Files\PostgreSQL")
    for version in ("18", "17", "16", "15", "14", "13", "12"):
        candidate = postgres_root / version / "bin" / f"{tool_name}.exe"
        if candidate.exists():
            return str(candidate)

    raise FileNotFoundError(
        f"No se encontro {tool_name}. Agrega PostgreSQL al PATH o define {env_var}."
    )


def postgres_env():
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_CONFIG["password"]
    # Neon.tech requiere SSL
    env["PGSSLMODE"] = DB_CONFIG.get("sslmode", "require")
    return env


# ===========================
# FUNCIONES DE BASE DE DATOS
# ===========================

def get_db_connection(database=None):
    """Obtener conexion a PostgreSQL / Neon.tech (cloud)."""
    try:
        config = DB_CONFIG.copy()
        if database:
            config["database"] = database
        return psycopg2.connect(**config)
    except Exception as e:
        print(f"Error conectando a la base de datos: {e}")
        return None


def get_database_info():
    """Obtener informacion de la base de datos configurada."""
    try:
        conn = get_db_connection()
        if not conn:
            return None

        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name;
            """
        )
        tables = [row[0] for row in cur.fetchall()]

        stats = {}
        # Contar registros de TODAS las tablas automáticamente
        for table in tables:
            try:
                cur.execute(sql.SQL("SELECT COUNT(*) FROM {};").format(sql.Identifier(table)))
                count = cur.fetchone()[0]
                stats[table] = count
            except Exception as e:
                print(f"Error contando registros de {table}: {e}")
                stats[table] = 0

        cur.close()
        conn.close()

        return {
            "tables": tables,
            "stats": stats,
        }
    except Exception as e:
        print(f"Error obteniendo informacion de la BD: {e}")
        return None


def get_available_databases():
    """Obtener lista de bases de datos disponibles.
    En Neon.tech (cloud) solo existe la base de datos configurada.
    """
    # Neon.tech es una BD gestionada: no se permite conectar a 'postgres'
    # ni crear/listar otras bases de datos. Solo existe neondb.
    return [DB_CONFIG["database"]]


def switch_database(new_database_name):
    """Cambiar a una base de datos diferente.
    En Neon.tech solo existe una base de datos (neondb).
    """
    neon_db = DB_CONFIG["database"]
    if new_database_name != neon_db:
        return False, (
            f"En Neon.tech solo está disponible la base de datos '{neon_db}'. "
            f"No se puede cambiar a '{new_database_name}'."
        )
    # Ya estamos en la única BD disponible
    config = load_config()
    config["current_database"] = neon_db
    save_config(config)
    return True, f"Base de datos activa: '{neon_db}'"


# ===========================
# FUNCIONES DE BACKUP
# ===========================

def generate_native_sql_dump(backup_file: Path, temp_file: Path) -> bool:
    """Genera un volcado SQL completo de Neon.tech usando psycopg2 (sin depender de pg_dump).
    Compatible con cualquier versión de PostgreSQL.
    """
    conn = get_db_connection()
    if not conn:
        raise Exception("No se pudo conectar a Neon.tech para generar el backup")

    cur = conn.cursor()
    lines = []
    lines.append("-- ========================================================")
    lines.append(f"-- Backup Neon.tech PostgreSQL - {DB_CONFIG['database']}")
    lines.append(f"-- Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"-- Host: {DB_CONFIG['host']}")
    lines.append("-- ========================================================\n")
    lines.append("SET statement_timeout = 0;")
    lines.append("SET client_encoding = 'UTF8';")
    lines.append("SET standard_conforming_strings = on;\n")

    # Obtener tablas base
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name;
    """)
    tables = [r[0] for r in cur.fetchall()]

    # Definiciones de tablas
    for table in tables:
        cur.execute("""
            SELECT column_name, data_type, character_maximum_length,
                   numeric_precision, numeric_scale, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position;
        """, (table,))
        columns = cur.fetchall()
        col_defs = []
        for col_name, data_type, char_len, num_prec, num_scale, is_nullable, col_default in columns:
            type_str = data_type.upper()
            if data_type in ('character varying', 'varchar') and char_len:
                type_str = f"VARCHAR({char_len})"
            elif data_type == 'numeric' and num_prec:
                type_str = f"NUMERIC({num_prec}, {num_scale})" if num_scale else f"NUMERIC({num_prec})"
            if col_default and 'nextval' in col_default and 'seq' in col_default:
                type_str = "BIGSERIAL" if data_type == 'bigint' else "SERIAL"
                col_default = None
            part = f'"{col_name}" {type_str}'
            if is_nullable == 'NO':
                part += " NOT NULL"
            if col_default:
                part += f" DEFAULT {col_default}"
            col_defs.append(part)

        # Clave primaria
        cur.execute("""
            SELECT kcu.column_name FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_name = %s
            ORDER BY kcu.ordinal_position;
        """, (table,))
        pk_cols = [r[0] for r in cur.fetchall()]
        if pk_cols:
            col_defs.append(f'PRIMARY KEY ({", ".join(pk_cols)})')

        lines.append(f'-- Table: {table}')
        lines.append(f'CREATE TABLE IF NOT EXISTS "{table}" (\n    ' + ",\n    ".join(col_defs) + "\n);")
        lines.append("")

    # Datos de cada tabla
    for table in tables:
        cur.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table)))
        rows = cur.fetchall()
        if rows:
            lines.append(f"-- Data for {table} ({len(rows)} rows)")
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position;
            """, (table,))
            col_names = [r[0] for r in cur.fetchall()]
            cols_str = ", ".join(f'"{c}"' for c in col_names)
            for row in rows:
                val_strs = []
                for val in row:
                    if val is None:
                        val_strs.append("NULL")
                    elif isinstance(val, bool):
                        val_strs.append("TRUE" if val else "FALSE")
                    elif isinstance(val, (int, float)):
                        val_strs.append(str(val))
                    else:
                        escaped = str(val).replace("'", "''")
                        val_strs.append(f"'{escaped}'")
                lines.append(f'INSERT INTO "{table}" ({cols_str}) VALUES ({", ".join(val_strs)});')
            lines.append("")

    # Llaves foráneas
    fk_statements = []
    for table in tables:
        cur.execute("""
            SELECT tc.constraint_name, kcu.column_name,
                   ccu.table_name AS foreign_table, ccu.column_name AS foreign_col
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = %s;
        """, (table,))
        for fk_name, col_name, f_table, f_col in cur.fetchall():
            fk_statements.append(
                f'DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = \'{fk_name}\') THEN '
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{fk_name}" FOREIGN KEY ("{col_name}") REFERENCES "{f_table}" ("{f_col}"); '
                f'END IF; END $$;'
            )
    if fk_statements:
        lines.append("-- Foreign Keys")
        lines.extend(fk_statements)
        lines.append("")

    # Reinicio de secuencias
    lines.append("-- Reset Sequences")
    for table in tables:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_default LIKE 'nextval%%'
        """, (table,))
        for (sc,) in cur.fetchall():
            lines.append(
                f"SELECT setval(pg_get_serial_sequence('\"{ table}\"', '{sc}'), "
                f"COALESCE((SELECT MAX(\"{sc}\") FROM \"{table}\"), 1), "
                f"(SELECT COUNT(*) > 0 FROM \"{table}\"));"
            )
    lines.append("")

    content = "\n".join(lines)
    with open(str(temp_file), "w", encoding="utf-8") as f:
        f.write(content)

    cur.close()
    conn.close()
    return True


def perform_backup(filename=DEFAULT_BACKUP_FILENAME):
    """Realizar backup contra Neon.tech, guardarlo localmente y subirlo a Cloudflare R2."""
    backup_filename = normalize_backup_filename(filename)
    backup_file = get_backup_path(backup_filename)
    temp_file = backup_file.with_name(f"{backup_file.stem}.tmp.sql")

    try:
        if temp_file.exists():
            temp_file.unlink()

        print(f"[DEBUG] Generando backup de {DB_CONFIG['database']} (Neon.tech) -> {backup_file}")

        # Intentar primero con pg_dump; si falla por versión, usar volcado nativo
        try:
            pg_dump_path = find_postgres_tool("pg_dump")
            command = [
                pg_dump_path,
                "-h", DB_CONFIG["host"],
                "-p", str(DB_CONFIG["port"]),
                "-U", DB_CONFIG["user"],
                "-d", DB_CONFIG["database"],
                "-f", str(temp_file),
                "--no-owner",
                "--no-privileges",
            ]
            result = subprocess.run(
                command,
                env=postgres_env(),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                # Si el error es por versión incompatible, usar volcado nativo
                stderr = result.stderr.strip()
                if "no coincide la versión" in stderr or "version mismatch" in stderr.lower():
                    print(f"[INFO] pg_dump versión incompatible, usando volcado nativo Python...")
                    generate_native_sql_dump(backup_file, temp_file)
                else:
                    if temp_file.exists():
                        temp_file.unlink()
                    print(f"Error en pg_dump: {stderr}")
                    return False, stderr
            # Verificar que el archivo temp tiene contenido
            if not temp_file.exists() or temp_file.stat().st_size == 0:
                print("[INFO] pg_dump generó archivo vacío, usando volcado nativo Python...")
                generate_native_sql_dump(backup_file, temp_file)
        except FileNotFoundError:
            print("[INFO] pg_dump no encontrado, usando volcado nativo Python...")
            generate_native_sql_dump(backup_file, temp_file)

        # Mover temp a backup final
        if temp_file.exists():
            os.replace(temp_file, backup_file)

        backup_size = backup_file.stat().st_size if backup_file.exists() else 0
        now_iso = datetime.now().isoformat()

        # Actualizar config local
        config = load_config()
        config["last_backup_time"] = now_iso
        config["backup_size"] = backup_size
        config["backup_filename"] = backup_filename
        config["backup_file"] = str(backup_file)
        config["backup_database"] = DB_CONFIG["database"]
        config["current_database"] = DB_CONFIG["database"]

        # Subir a Cloudflare R2 en hilo separado para no bloquear el scheduler
        def _upload_to_b2():
            try:
                result = b2_service.upload_file_to_b2(
                    local_path=str(backup_file),
                    remote_filename=f"{backup_filename}.sql",  # nombre real configurado
                )
                config["r2_last_upload"] = result["upload_time"]
                config["r2_file"] = result["file_name"]
                config["r2_status"] = "ok"
                save_config(config)
                print(
                    f"[R2] Backup subido a Cloudflare R2: {result['file_name']} "
                    f"({result['size']} bytes)"
                )
            except Exception as b2_err:
                config["r2_status"] = f"error: {b2_err}"
                save_config(config)
                print(f"[R2] Error al subir a Cloudflare R2: {b2_err}")

        save_config(config)
        threading.Thread(target=_upload_to_b2, daemon=True).start()

        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Backup realizado: {backup_file} ({backup_size} bytes)"
        )
        return True, str(backup_file)
    except Exception as e:
        if temp_file.exists():
            temp_file.unlink()
        print(f"Error realizando backup: {e}")
        return False, str(e)


def restore_backup(filename=None, new_database_name=None, from_b2=False):
    """Restaurar un backup en Neon.tech.

    Proceso:
    1. Si from_b2=True o el archivo local no existe, descarga 'backup.sql' desde Cloudflare R2.
    2. Hace TRUNCATE de todas las tablas existentes (CASCADE).
    3. Ejecuta el SQL del backup con psql contra Neon.tech.
    """
    config = load_config()
    backup_filename = normalize_backup_filename(
        filename or config.get("backup_filename") or DEFAULT_BACKUP_FILENAME
    )
    backup_file = get_backup_path(backup_filename)
    target_database = DB_CONFIG["database"]

    # ── Intentar descargar desde Cloudflare R2 si se solicita o no hay copia local ──
    if from_b2 or not backup_file.exists():
        print("[R2] Descargando 'backup.sql' desde Cloudflare R2...")
        try:
            b2_service.download_file_from_b2(
                remote_filename="backup.sql",  # nombre fijo en R2
                target_path=str(backup_file),
            )
            print(f"[R2] Descarga completada: {backup_file}")
        except Exception as b2_err:
            if not backup_file.exists():
                return False, (
                    f"❌ No se encontró el backup en Cloudflare R2 ni en local: {b2_err}"
                )
            print(f"[R2] Advertencia: no se pudo descargar desde R2, usando copia local. {b2_err}")

    # ── Validar archivo local ──
    if not backup_file.exists():
        return False, (
            f"❌ Error: El archivo de backup '{backup_filename}.sql' no existe en {BACKUP_DIR}. "
            f"Verifica el nombre e intenta nuevamente."
        )

    try:
        file_size = backup_file.stat().st_size
        if file_size == 0:
            return False, (
                f"❌ Error: El archivo '{backup_filename}.sql' está vacío. "
                f"No se puede restaurar un backup sin datos."
            )
    except Exception as e:
        return False, f"❌ Error al verificar el archivo '{backup_filename}.sql': {str(e)}"

    try:
        psql_path = find_postgres_tool("psql")

        # ── Paso 1: Truncar tablas ──
        print(f"[DEBUG] Truncando tablas en Neon.tech ({target_database})...")
        conn = get_db_connection()
        if not conn:
            return False, "No se pudo conectar a Neon.tech para truncar las tablas."

        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename;
            """
        )
        tables = [row[0] for row in cur.fetchall()]
        if tables:
            tables_sql = ", ".join(f'"{t}"' for t in tables)
            cur.execute(f"TRUNCATE TABLE {tables_sql} RESTART IDENTITY CASCADE;")
            print(f"[DEBUG] Tablas truncadas: {tables}")
        cur.close()
        conn.close()

        # ── Paso 2: Restaurar con psql ──
        command = [
            psql_path,
            "-h", DB_CONFIG["host"],
            "-p", str(DB_CONFIG["port"]),
            "-U", DB_CONFIG["user"],
            "-d", target_database,
            "-v", "ON_ERROR_STOP=0",
            "-f", str(backup_file),
        ]
        print(f"[DEBUG] Restaurando {backup_file} en Neon.tech ({target_database})")
        result = subprocess.run(
            command,
            env=postgres_env(),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error_message = result.stderr.strip() or "psql termino con error"
            print(f"Error en restauracion: {error_message}")
            return False, error_message

        config["current_database"] = target_database
        config["backup_filename"] = backup_filename
        config["backup_file"] = str(backup_file)
        config["last_restore_time"] = datetime.now().isoformat()
        save_config(config)

        message = (
            f"Base de datos '{target_database}' (Neon.tech) restaurada con "
            f"'{backup_filename}.sql' exitosamente"
        )
        print(message)
        return True, message
    except Exception as e:
        print(f"Error restaurando backup: {e}")
        return False, str(e)


# ===========================
# FUNCIONES DEL SCHEDULER
# ===========================

def get_current_backup_filename():
    """Obtener el nombre actual del backup desde el config."""
    config = load_config()
    return normalize_backup_filename(config.get("backup_filename", DEFAULT_BACKUP_FILENAME))


def start_scheduler(interval_minutes=None, interval_seconds=None, interval_hours=None,
                   interval_days=None, interval_weeks=None, backup_filename=DEFAULT_BACKUP_FILENAME):
    """Iniciar o reconfigurar los backups automaticos.

    Al iniciar, ejecuta un primer backup de inmediato (en hilo separado)
    y programa los sucesivos según el intervalo configurado.
    """
    global scheduler_running, CURRENT_BACKUP_FILENAME

    backup_filename = normalize_backup_filename(backup_filename)
    CURRENT_BACKUP_FILENAME = backup_filename

    scheduler.remove_all_jobs()

    # Función que siempre usa el nombre más reciente del config
    def backup_with_current_name():
        current_name = get_current_backup_filename()
        return perform_backup(current_name)

    # Determinar unidad de intervalo (prioridad: segundos > minutos > horas > días > semanas)
    interval_type = "minuto(s)"
    interval_value = 1
    trigger_params = {}

    if interval_seconds is not None and interval_seconds > 0:
        trigger_params = {"seconds": interval_seconds}
        interval_type = "segundo(s)"
        interval_value = interval_seconds
        config_key = "backup_interval_seconds"
    elif interval_minutes is not None and interval_minutes > 0:
        trigger_params = {"minutes": interval_minutes}
        interval_type = "minuto(s)"
        interval_value = interval_minutes
        config_key = "backup_interval_minutes"
    elif interval_hours is not None and interval_hours > 0:
        trigger_params = {"hours": interval_hours}
        interval_type = "hora(s)"
        interval_value = interval_hours
        config_key = "backup_interval_hours"
    elif interval_days is not None and interval_days > 0:
        trigger_params = {"days": interval_days}
        interval_type = "día(s)"
        interval_value = interval_days
        config_key = "backup_interval_days"
    elif interval_weeks is not None and interval_weeks > 0:
        trigger_params = {"weeks": interval_weeks}
        interval_type = "semana(s)"
        interval_value = interval_weeks
        config_key = "backup_interval_weeks"
    else:
        trigger_params = {"minutes": 1}
        interval_type = "minuto(s)"
        interval_value = 1
        config_key = "backup_interval_minutes"

    scheduler.add_job(
        func=backup_with_current_name,
        trigger="interval",
        **trigger_params,
        id="backup_job",
        replace_existing=True,
    )

    if not scheduler.running:
        scheduler.start()

    scheduler_running = True

    config = load_config()
    config["backup_interval_seconds"] = None
    config["backup_interval_minutes"] = None
    config["backup_interval_hours"] = None
    config["backup_interval_days"] = None
    config["backup_interval_weeks"] = None
    config[config_key] = interval_value
    config["backup_filename"] = backup_filename
    config["backup_file"] = str(get_backup_path(backup_filename))
    config["current_database"] = DB_CONFIG["database"]
    save_config(config)

    # Primer backup inmediato al iniciar (en hilo para no bloquear la respuesta HTTP)
    threading.Thread(target=backup_with_current_name, daemon=True).start()

    print(
        f"Scheduler iniciado: backup cada {interval_value} {interval_type} -> "
        f"{get_backup_path(backup_filename)} + Cloudflare R2"
    )
    return True


def stop_scheduler():
    """Detener los backups automaticos."""
    global scheduler_running

    if scheduler_running:
        scheduler.remove_all_jobs()
        scheduler_running = False
        print("Scheduler detenido")
        return True
    return False


def get_next_run_time():
    if not scheduler.running:
        return None

    job = scheduler.get_job("backup_job")
    if not job or not job.next_run_time:
        return None

    return job.next_run_time.isoformat()


# ===========================
# RUTAS DE LA API
# ===========================

@app.route("/api/status", methods=["GET"])
def status():
    return jsonify(
        {
            "status": "ok",
            "message": "Servidor Flask funcionando correctamente",
            "timestamp": datetime.now().isoformat(),
        }
    )


@app.route("/api/database/info", methods=["GET"])
def database_info():
    info = get_database_info()

    if info:
        return jsonify(
            {
                "success": True,
                "tables": info["tables"],
                "stats": info["stats"],
                "database_name": DB_CONFIG["database"],
            }
        )

    return (
        jsonify(
            {
                "success": False,
                "message": "Error al obtener informacion de la base de datos",
                "database_name": DB_CONFIG["database"],
            }
        ),
        500,
    )


@app.route("/api/database/list", methods=["GET"])
def list_databases():
    """Listar todas las bases de datos disponibles en PostgreSQL."""
    databases = get_available_databases()
    return jsonify(
        {
            "success": True,
            "databases": databases,
            "current_database": DB_CONFIG["database"],
        }
    )


@app.route("/api/database/switch", methods=["POST"])
def switch_database_route():
    """Cambiar a una base de datos diferente."""
    data = request.get_json() or {}
    new_database = data.get("database_name")
    
    if not new_database:
        return jsonify(
            {
                "success": False,
                "message": "Debe proporcionar el nombre de la base de datos"
            }
        ), 400
    
    success, message = switch_database(new_database)
    
    if success:
        return jsonify(
            {
                "success": True,
                "message": message,
                "database_name": DB_CONFIG["database"],
            }
        )
    
    return jsonify({"success": False, "message": message}), 500


@app.route("/api/scheduler/status", methods=["GET"])
def scheduler_status():
    config = load_config()
    backup_filename = normalize_backup_filename(
        config.get("backup_filename") or CURRENT_BACKUP_FILENAME
    )
    
    # Obtener intervalo y unidad
    interval_seconds = config.get("backup_interval_seconds")
    interval_minutes = config.get("backup_interval_minutes")
    interval_hours = config.get("backup_interval_hours")
    interval_days = config.get("backup_interval_days")
    interval_weeks = config.get("backup_interval_weeks")
    
    # Determinar cual está activo
    interval_value = None
    interval_unit = "minutes"
    
    if interval_seconds is not None:
        interval_value = interval_seconds
        interval_unit = "seconds"
    elif interval_minutes is not None:
        interval_value = interval_minutes
        interval_unit = "minutes"
    elif interval_hours is not None:
        interval_value = interval_hours
        interval_unit = "hours"
    elif interval_days is not None:
        interval_value = interval_days
        interval_unit = "days"
    elif interval_weeks is not None:
        interval_value = interval_weeks
        interval_unit = "weeks"
    else:
        interval_value = 1
        interval_unit = "minutes"

    return jsonify(
        {
            "success": True,
            "is_running": scheduler_running,
            "interval_value": interval_value,
            "interval_unit": interval_unit,
            "backup_filename": backup_filename,
            "backup_file": str(get_backup_path(backup_filename)),
            "next_run_time": get_next_run_time(),
        }
    )


@app.route("/api/scheduler/start", methods=["POST"])
def start_scheduler_route():
    data = request.get_json() or {}
    backup_filename = normalize_backup_filename(
        data.get("backup_filename", DEFAULT_BACKUP_FILENAME)
    )
    
    # Obtener el intervalo y unidad
    interval_value = data.get("interval_value")
    interval_unit = data.get("interval_unit", "minutes")
    
    if interval_value is None:
        # Compatibilidad con versión anterior
        interval_value = data.get("interval_minutes") or data.get("interval_seconds") or 1
        if data.get("interval_seconds"):
            interval_unit = "seconds"
    
    interval_value = int(interval_value)
    
    # Validar según la unidad
    validation_limits = {
        "seconds": (1, 3600, "El intervalo en segundos debe estar entre 1 y 3600"),
        "minutes": (1, 1440, "El intervalo en minutos debe estar entre 1 y 1440 (24 horas)"),
        "hours": (1, 720, "El intervalo en horas debe estar entre 1 y 720 (30 días)"),
        "days": (1, 365, "El intervalo en días debe estar entre 1 y 365"),
        "weeks": (1, 52, "El intervalo en semanas debe estar entre 1 y 52"),
    }
    
    if interval_unit in validation_limits:
        min_val, max_val, error_msg = validation_limits[interval_unit]
        if interval_value < min_val or interval_value > max_val:
            return jsonify({"success": False, "message": error_msg}), 400
    
    # Mapear unidades a nombres en español
    unit_names = {
        "seconds": "segundo(s)",
        "minutes": "minuto(s)",
        "hours": "hora(s)",
        "days": "día(s)",
        "weeks": "semana(s)",
    }
    
    # Iniciar el scheduler con la unidad correcta
    kwargs = {"backup_filename": backup_filename}
    if interval_unit == "seconds":
        kwargs["interval_seconds"] = interval_value
    elif interval_unit == "minutes":
        kwargs["interval_minutes"] = interval_value
    elif interval_unit == "hours":
        kwargs["interval_hours"] = interval_value
    elif interval_unit == "days":
        kwargs["interval_days"] = interval_value
    elif interval_unit == "weeks":
        kwargs["interval_weeks"] = interval_value
    else:
        kwargs["interval_minutes"] = interval_value
        interval_unit = "minutes"
    
    start_scheduler(**kwargs)
    
    unit_text = unit_names.get(interval_unit, "minuto(s)")
    message = f"Scheduler iniciado: backup cada {interval_value} {unit_text}"

    return jsonify(
        {
            "success": True,
            "message": message,
            "backup_filename": backup_filename,
            "backup_file": str(get_backup_path(backup_filename)),
            "next_run_time": get_next_run_time(),
        }
    )


@app.route("/api/scheduler/stop", methods=["POST"])
def stop_scheduler_route():
    if stop_scheduler():
        return jsonify({"success": True, "message": "Scheduler detenido"})

    return (
        jsonify(
            {
                "success": False,
                "message": "El scheduler no esta en ejecucion",
            }
        ),
        400,
    )


@app.route("/api/scheduler/update-filename", methods=["POST"])
def update_backup_filename_route():
    """Actualizar el nombre del archivo de backup sin reiniciar el scheduler."""
    data = request.get_json() or {}
    new_filename = normalize_backup_filename(
        data.get("backup_filename", DEFAULT_BACKUP_FILENAME)
    )
    
    config = load_config()
    config["backup_filename"] = new_filename
    config["backup_file"] = str(get_backup_path(new_filename))
    save_config(config)
    
    return jsonify({
        "success": True,
        "message": f"Nombre de backup actualizado a: {new_filename}",
        "backup_filename": new_filename,
        "backup_file": str(get_backup_path(new_filename))
    })


@app.route("/api/scheduler/update-interval", methods=["POST"])
def update_interval_route():
    """Actualizar el intervalo del scheduler sin detenerlo."""
    if not scheduler_running:
        return jsonify({
            "success": False,
            "message": "El scheduler no esta en ejecucion"
        }), 400
    
    data = request.get_json() or {}
    interval_value = data.get("interval_value")
    interval_unit = data.get("interval_unit", "minutes")
    
    if interval_value is None:
        return jsonify({
            "success": False,
            "message": "Debe proporcionar interval_value"
        }), 400
    
    interval_value = int(interval_value)
    
    # Validar según la unidad
    validation_limits = {
        "seconds": (1, 3600, "El intervalo en segundos debe estar entre 1 y 3600"),
        "minutes": (1, 1440, "El intervalo en minutos debe estar entre 1 y 1440"),
        "hours": (1, 720, "El intervalo en horas debe estar entre 1 y 720"),
        "days": (1, 365, "El intervalo en días debe estar entre 1 y 365"),
        "weeks": (1, 52, "El intervalo en semanas debe estar entre 1 y 52"),
    }
    
    if interval_unit in validation_limits:
        min_val, max_val, error_msg = validation_limits[interval_unit]
        if interval_value < min_val or interval_value > max_val:
            return jsonify({"success": False, "message": error_msg}), 400
    
    # Obtener el nombre actual del backup
    config = load_config()
    backup_filename = config.get("backup_filename", DEFAULT_BACKUP_FILENAME)
    
    # Reiniciar el scheduler con el nuevo intervalo
    kwargs = {"backup_filename": backup_filename}
    if interval_unit == "seconds":
        kwargs["interval_seconds"] = interval_value
    elif interval_unit == "minutes":
        kwargs["interval_minutes"] = interval_value
    elif interval_unit == "hours":
        kwargs["interval_hours"] = interval_value
    elif interval_unit == "days":
        kwargs["interval_days"] = interval_value
    elif interval_unit == "weeks":
        kwargs["interval_weeks"] = interval_value
    
    start_scheduler(**kwargs)
    
    unit_names = {
        "seconds": "segundo(s)",
        "minutes": "minuto(s)",
        "hours": "hora(s)",
        "days": "día(s)",
        "weeks": "semana(s)",
    }
    unit_text = unit_names.get(interval_unit, "minuto(s)")
    
    return jsonify({
        "success": True,
        "message": f"Intervalo actualizado a {interval_value} {unit_text}",
        "interval_value": interval_value,
        "interval_unit": interval_unit,
        "next_run_time": get_next_run_time()
    })


@app.route("/api/backup/info", methods=["GET"])
def backup_info():
    config = load_config()
    requested_filename = request.args.get("backup_filename")
    backup_filename = normalize_backup_filename(
        requested_filename or config.get("backup_filename") or CURRENT_BACKUP_FILENAME
    )
    backup_file = get_backup_path(backup_filename)

    backup_size = backup_file.stat().st_size if backup_file.exists() else 0
    if not backup_file.exists():
        last_backup_time = None
    elif backup_filename != config.get("backup_filename"):
        last_backup_time = datetime.fromtimestamp(backup_file.stat().st_mtime).isoformat()
    else:
        last_backup_time = config.get("last_backup_time")

    return jsonify(
        {
            "success": True,
            "last_backup_time": last_backup_time,
            "backup_size": backup_size,
            "backup_filename": backup_filename,
            "backup_file": str(backup_file),
            "backup_exists": backup_file.exists(),
        }
    )


@app.route("/api/backup/manual", methods=["POST"])
def manual_backup():
    data = request.get_json() or {}
    backup_filename = normalize_backup_filename(
        data.get("backup_filename", DEFAULT_BACKUP_FILENAME)
    )

    success, result = perform_backup(backup_filename)
    if success:
        return jsonify(
            {
                "success": True,
                "message": "Backup realizado exitosamente",
                "backup_filename": backup_filename,
                "backup_file": result,
                "timestamp": datetime.now().isoformat(),
            }
        )

    return jsonify({"success": False, "message": result}), 500


@app.route("/api/backup/restore", methods=["POST"])
def restore_backup_route():
    data = request.get_json() or {}
    config = load_config()
    backup_filename = normalize_backup_filename(
        data.get("backup_filename") or config.get("backup_filename") or DEFAULT_BACKUP_FILENAME
    )
    new_database_name = data.get("new_database_name")
    # Si el archivo local no existe, intentar descargarlo desde B2 automáticamente
    backup_file = get_backup_path(backup_filename)
    from_b2 = data.get("from_b2", False) or not backup_file.exists()

    success, message = restore_backup(backup_filename, new_database_name, from_b2=from_b2)
    if success:
        updated_info = {}
        try:
            db_info = get_database_info()
            if db_info:
                updated_info = {"stats": db_info["stats"], "tables": db_info["tables"]}
        except Exception:
            pass
        return jsonify(
            {
                "success": True,
                "message": message,
                "database_name": DB_CONFIG["database"],
                "backup_filename": backup_filename,
                "backup_file": str(backup_file),
                "timestamp": datetime.now().isoformat(),
                **updated_info,
            }
        )

    return jsonify({"success": False, "message": message}), 500


@app.route("/api/b2/status", methods=["GET"])
def b2_status_route():
    """Retorna el estado de conexión con Cloudflare R2 y el último archivo subido."""
    status = b2_service.get_b2_status()
    config = load_config()
    status["r2_last_upload"] = config.get("r2_last_upload")
    status["r2_file"] = config.get("r2_file")
    status["r2_status"] = config.get("r2_status", "unknown")
    return jsonify({"success": True, **status})


@app.route("/api/backup/validate", methods=["POST"])
def validate_backup():
    """Validar que un archivo de backup existe y es válido antes de restaurar."""
    data = request.get_json() or {}
    backup_filename = normalize_backup_filename(
        data.get("backup_filename", DEFAULT_BACKUP_FILENAME)
    )
    backup_file = get_backup_path(backup_filename)
    
    if not backup_file.exists():
        return jsonify({
            "success": False,
            "exists": False,
            "message": f"El archivo '{backup_filename}.sql' no existe",
            "backup_filename": backup_filename
        }), 404
    
    try:
        file_size = backup_file.stat().st_size
        if file_size == 0:
            return jsonify({
                "success": False,
                "exists": True,
                "message": f"El archivo '{backup_filename}.sql' está vacío",
                "backup_filename": backup_filename,
                "file_size": 0
            }), 400
        
        file_date = datetime.fromtimestamp(backup_file.stat().st_mtime).isoformat()
        
        return jsonify({
            "success": True,
            "exists": True,
            "message": "Archivo válido",
            "backup_filename": backup_filename,
            "backup_file": str(backup_file),
            "file_size": file_size,
            "file_date": file_date
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "exists": True,
            "message": f"Error al validar el archivo: {str(e)}",
            "backup_filename": backup_filename
        }), 500


@app.route("/api/backup/upload", methods=["POST"])
def upload_backup():
    """Cargar un archivo de backup desde el cliente."""
    if "file" not in request.files:
        return jsonify({"success": False, "message": "No se proporcionó ningún archivo"}), 400
    
    file = request.files["file"]
    
    if file.filename == "":
        return jsonify({"success": False, "message": "No se seleccionó ningún archivo"}), 400
    
    # Validar extensión
    if not file.filename.lower().endswith(".sql"):
        return jsonify({
            "success": False,
            "message": "Solo se permiten archivos .sql"
        }), 400
    
    # Normalizar nombre del archivo
    original_filename = file.filename[:-4] if file.filename.endswith(".sql") else file.filename
    backup_filename = normalize_backup_filename(original_filename)
    backup_file = get_backup_path(backup_filename)
    
    try:
        # Guardar el archivo
        file.save(str(backup_file))
        
        # Verificar que se guardó correctamente
        file_size = backup_file.stat().st_size
        if file_size == 0:
            backup_file.unlink()  # Eliminar archivo vacío
            return jsonify({
                "success": False,
                "message": "El archivo subido está vacío"
            }), 400
        
        return jsonify({
            "success": True,
            "message": f"Archivo '{backup_filename}.sql' subido exitosamente",
            "backup_filename": backup_filename,
            "backup_file": str(backup_file),
            "file_size": file_size,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error al subir el archivo: {str(e)}"
        }), 500


@app.route("/api/config", methods=["GET"])
def get_config():
    config = load_config()
    return jsonify({"success": True, "config": config})


@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.get_json() or {}
    config = load_config()

    if "backup_interval_minutes" in data:
        interval = int(data["backup_interval_minutes"])
        if interval < 1 or interval > 60:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "El intervalo debe estar entre 1 y 60 minutos",
                    }
                ),
                400,
            )
        config["backup_interval_minutes"] = interval

    if "backup_filename" in data:
        config["backup_filename"] = normalize_backup_filename(data["backup_filename"])
        config["backup_file"] = str(get_backup_path(config["backup_filename"]))

    save_config(config)

    if scheduler_running:
        start_scheduler(
            config.get("backup_interval_minutes", 1),
            config.get("backup_filename", DEFAULT_BACKUP_FILENAME),
        )

    return jsonify(
        {
            "success": True,
            "message": "Configuracion actualizada",
            "config": config,
        }
    )


# ===========================
# INICIALIZACION DE ESQUEMA
# ===========================

def initialize_schema():
    """Crear tablas e insertar datos de ejemplo si no existen (primera ejecucion).
    Se ejecuta automaticamente al arrancar el backend.
    """
    print("Verificando esquema de base de datos en Neon.tech...")
    conn = get_db_connection()
    if not conn:
        print("[ERROR] No se pudo conectar a Neon.tech para inicializar el esquema.")
        return

    try:
        cur = conn.cursor()

        # Crear tablas si no existen
        cur.execute("""
            CREATE TABLE IF NOT EXISTS productos (
                id_producto SERIAL PRIMARY KEY,
                nombre VARCHAR(255) NOT NULL,
                precio DECIMAL(10, 2) NOT NULL,
                stock INTEGER NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id_cliente SERIAL PRIMARY KEY,
                nombre VARCHAR(100) NOT NULL,
                apellido VARCHAR(100) NOT NULL,
                email VARCHAR(255) UNIQUE
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id_pedido SERIAL PRIMARY KEY,
                id_cliente INTEGER NOT NULL,
                id_producto INTEGER NOT NULL,
                cantidad INTEGER NOT NULL,
                fecha_pedido DATE NOT NULL,
                FOREIGN KEY (id_cliente) REFERENCES clientes(id_cliente),
                FOREIGN KEY (id_producto) REFERENCES productos(id_producto)
            );
        """)

        print("[OK] Tablas verificadas/creadas correctamente.")

        # Insertar datos de ejemplo solo si las tablas estan vacias
        cur.execute("SELECT COUNT(*) FROM productos;")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO productos (nombre, precio, stock) VALUES
                ('Laptop', 850.00, 20),
                ('Raton Inalambrico', 25.50, 100),
                ('Teclado Mecanico', 75.00, 50);
            """)
            print("[OK] Datos de ejemplo insertados en 'productos'.")

        cur.execute("SELECT COUNT(*) FROM clientes;")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO clientes (nombre, apellido, email) VALUES
                ('Ana', 'Garcia', 'ana.garcia@gmail.com'),
                ('Luis', 'Rodriguez', 'luis.rodriguez@gmail.com');
            """)
            print("[OK] Datos de ejemplo insertados en 'clientes'.")

        cur.execute("SELECT COUNT(*) FROM pedidos;")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO pedidos (id_cliente, id_producto, cantidad, fecha_pedido) VALUES
                (1, 1, 1, '2024-05-20'),
                (1, 2, 2, '2024-05-20'),
                (2, 3, 1, '2024-05-21');
            """)
            print("[OK] Datos de ejemplo insertados en 'pedidos'.")

        conn.commit()
        print("[OK] Esquema inicializado correctamente en Neon.tech.")
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Error inicializando esquema: {e}")
    finally:
        cur.close()
        conn.close()


# ===========================
# INICIALIZACION
# ===========================

if __name__ == "__main__":
    print("=" * 60)
    print("Sistema de Backups Automaticos - Neon.tech (Cloud)")
    print("=" * 60)
    print(f"Host:           {DB_CONFIG['host']}")
    print(f"Base de datos:  {DB_CONFIG['database']}")
    print(f"Usuario:        {DB_CONFIG['user']}")
    print(f"SSL:            {DB_CONFIG.get('sslmode', 'require')}")
    print(f"Backups en:     {BACKUP_DIR}")
    print("Servidor Flask: http://localhost:5000")
    print("=" * 60)

    # Verificar conexion a Neon.tech
    conn = get_db_connection()
    if conn:
        print("[OK] Conexion a Neon.tech exitosa")
        conn.close()

        # Inicializar esquema (crea tablas e inserta datos si es la primera vez)
        initialize_schema()
    else:
        print("[ERROR] Error al conectar con Neon.tech")
        print("Verifica tu conexion a internet y las credenciales.")

    print("=" * 60)
    print("Presiona Ctrl+C para detener el servidor")
    print("=" * 60)

    app.run(debug=True, host="0.0.0.0", port=5000)
