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


DB_CONFIG = {
    "host": "localhost",
    "database": load_database_name(),
    "user": "postgres",
    "password": "1234",
    "port": 5432,
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
    return env


# ===========================
# FUNCIONES DE BASE DE DATOS
# ===========================

def get_db_connection(database=None):
    """Obtener conexion a PostgreSQL."""
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
    """Obtener lista de bases de datos disponibles en PostgreSQL."""
    try:
        conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            database="postgres",
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            port=DB_CONFIG["port"],
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT datname 
            FROM pg_database 
            WHERE datistemplate = false 
            AND datname NOT IN ('postgres')
            ORDER BY datname;
            """
        )
        databases = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return databases
    except Exception as e:
        print(f"Error obteniendo bases de datos: {e}")
        return []


def switch_database(new_database_name):
    """Cambiar a una base de datos diferente."""
    try:
        # Verificar que la base de datos existe
        available_dbs = get_available_databases()
        if new_database_name not in available_dbs:
            return False, f"La base de datos '{new_database_name}' no existe en PostgreSQL"
        
        # Intentar conectar a la nueva base de datos
        test_conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            database=new_database_name,
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            port=DB_CONFIG["port"],
        )
        test_conn.close()
        
        # Actualizar configuración
        DB_CONFIG["database"] = new_database_name
        
        config = load_config()
        config["current_database"] = new_database_name
        save_config(config)
        
        return True, f"Cambiado a la base de datos '{new_database_name}'"
    except Exception as e:
        return False, f"Error al cambiar de base de datos: {str(e)}"


# ===========================
# FUNCIONES DE BACKUP
# ===========================

def perform_backup(filename=DEFAULT_BACKUP_FILENAME):
    """Realizar backup y reemplazar el archivo anterior con el mismo nombre."""
    backup_filename = normalize_backup_filename(filename)
    backup_file = get_backup_path(backup_filename)
    temp_file = backup_file.with_name(f"{backup_file.stem}.tmp.sql")

    try:
        pg_dump_path = find_postgres_tool("pg_dump")

        if temp_file.exists():
            temp_file.unlink()

        command = [
            pg_dump_path,
            "-h",
            DB_CONFIG["host"],
            "-p",
            str(DB_CONFIG["port"]),
            "-U",
            DB_CONFIG["user"],
            "-d",
            DB_CONFIG["database"],
            "-f",
            str(temp_file),
            "--no-owner",
            "--no-privileges",
        ]

        print(f"[DEBUG] Ejecutando backup de {DB_CONFIG['database']} en {backup_file}")
        result = subprocess.run(
            command,
            env=postgres_env(),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            if temp_file.exists():
                temp_file.unlink()
            error_message = result.stderr.strip() or "pg_dump termino con error"
            print(f"Error en backup: {error_message}")
            return False, error_message

        os.replace(temp_file, backup_file)
        backup_size = backup_file.stat().st_size if backup_file.exists() else 0

        config = load_config()
        config["last_backup_time"] = datetime.now().isoformat()
        config["backup_size"] = backup_size
        config["backup_filename"] = backup_filename
        config["backup_file"] = str(backup_file)
        # Guardar la base de origen permite restaurarla aunque haya sido
        # eliminada de PostgreSQL o aunque el nombre del archivo sea distinto.
        config["backup_database"] = DB_CONFIG["database"]
        config["current_database"] = DB_CONFIG["database"]
        save_config(config)

        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Backup realizado: {backup_file}"
        )
        return True, str(backup_file)
    except Exception as e:
        if temp_file.exists():
            temp_file.unlink()
        print(f"Error realizando backup: {e}")
        return False, str(e)


def restore_backup(filename=None, new_database_name=None):
    """Restaurar manualmente un backup en su base de datos de origen."""
    config = load_config()
    backup_filename = normalize_backup_filename(
        filename or config.get("backup_filename") or DEFAULT_BACKUP_FILENAME
    )
    backup_file = get_backup_path(backup_filename)

    # Nunca asumir que el nombre del archivo es el nombre de la BD.
    # El backup guarda este dato; el fallback mantiene compatibilidad con copias antiguas.
    is_last_backup = backup_filename == normalize_backup_filename(
        config.get("backup_filename") or DEFAULT_BACKUP_FILENAME
    )
    source_database = config.get("backup_database") if is_last_backup else None
    target_database = str(
        new_database_name or source_database
        or config.get("current_database") or DB_CONFIG["database"]
    ).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", target_database):
        return False, "El nombre de la base de datos de destino no es válido."
    if target_database.lower() == "postgres":
        return False, "No se puede reemplazar la base de datos administrativa 'postgres'."

    # Validación mejorada: verificar que el archivo existe
    if not backup_file.exists():
        return False, f"❌ Error: El archivo de backup '{backup_filename}.sql' no existe en {BACKUP_DIR}. Verifica el nombre e intenta nuevamente."

    # Validación adicional: verificar que el archivo tiene contenido
    try:
        file_size = backup_file.stat().st_size
        if file_size == 0:
            return False, f"❌ Error: El archivo '{backup_filename}.sql' está vacío. No se puede restaurar un backup sin datos."
    except Exception as e:
        return False, f"❌ Error al verificar el archivo '{backup_filename}.sql': {str(e)}"

    try:
        psql_path = find_postgres_tool("psql")

        conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            database="postgres",
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            port=DB_CONFIG["port"],
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Terminar todas las conexiones a la BD si existe
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s
              AND pid <> pg_backend_pid();
            """,
            (target_database,),
        )
        
        # Eliminar y crear la nueva base de datos
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {};").format(sql.Identifier(target_database)))
        cur.execute(sql.SQL("CREATE DATABASE {};").format(sql.Identifier(target_database)))

        cur.close()
        conn.close()

        command = [
            psql_path,
            "-h",
            DB_CONFIG["host"],
            "-p",
            str(DB_CONFIG["port"]),
            "-U",
            DB_CONFIG["user"],
            "-d",
            target_database,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(backup_file),
        ]

        print(f"[DEBUG] Restaurando {backup_file} en {target_database}")
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

        # ACTUALIZAR el sistema para usar la nueva base de datos
        DB_CONFIG["database"] = target_database

        config["current_database"] = target_database
        config["backup_filename"] = backup_filename
        config["backup_file"] = str(backup_file)
        config["last_restore_time"] = datetime.now().isoformat()
        save_config(config)

        message = (
            f"Base de datos '{target_database}' creada y restaurada con "
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
    """Iniciar o reconfigurar los backups automaticos."""
    global scheduler_running, CURRENT_BACKUP_FILENAME

    backup_filename = normalize_backup_filename(backup_filename)
    CURRENT_BACKUP_FILENAME = backup_filename

    scheduler.remove_all_jobs()
    
    # Usar una funcion que obtenga el nombre actual del config cada vez
    def backup_with_current_name():
        current_name = get_current_backup_filename()
        return perform_backup(current_name)
    
    # Determinar qué unidad usar (prioridad: segundos, minutos, horas, días, semanas)
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
        # Por defecto: 1 minuto
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
    # Limpiar todos los intervalos anteriores
    config["backup_interval_seconds"] = None
    config["backup_interval_minutes"] = None
    config["backup_interval_hours"] = None
    config["backup_interval_days"] = None
    config["backup_interval_weeks"] = None
    # Establecer solo el intervalo actual
    config[config_key] = interval_value
    config["backup_filename"] = backup_filename
    config["backup_file"] = str(get_backup_path(backup_filename))
    config["current_database"] = DB_CONFIG["database"]
    save_config(config)

    print(
        f"Scheduler iniciado: backup cada {interval_value} {interval_type} -> "
        f"{get_backup_path(backup_filename)}"
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
    # Sin nombre explícito se restaura la última copia configurada.
    backup_filename = normalize_backup_filename(
        data.get("backup_filename") or config.get("backup_filename") or DEFAULT_BACKUP_FILENAME
    )
    new_database_name = data.get("new_database_name")  # Opcional

    success, message = restore_backup(backup_filename, new_database_name)
    if success:
        return jsonify(
            {
                "success": True,
                "message": message,
                "database_name": DB_CONFIG["database"],
                "backup_filename": backup_filename,
                "backup_file": str(get_backup_path(backup_filename)),
                "timestamp": datetime.now().isoformat(),
            }
        )

    return jsonify({"success": False, "message": message}), 500


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
# INICIALIZACION
# ===========================

if __name__ == "__main__":
    print("=" * 60)
    print("Sistema de Backups Automaticos - PostgreSQL")
    print("=" * 60)
    print(f"Directorio de backups: {BACKUP_DIR}")
    print(f"Base de datos: {DB_CONFIG['database']}")
    print("Servidor Flask: http://localhost:5000")
    print("=" * 60)

    conn = get_db_connection()
    if conn:
        print("Conexion a PostgreSQL exitosa")
        conn.close()
    else:
        print("Error al conectar con PostgreSQL")
        print("Verifica que PostgreSQL este corriendo y la clave sea 1234")

    print("=" * 60)
    print("Presiona Ctrl+C para detener el servidor")
    print("=" * 60)

    app.run(debug=True, host="0.0.0.0", port=5000)
