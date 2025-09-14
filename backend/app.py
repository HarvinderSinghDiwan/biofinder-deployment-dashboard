from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from time import sleep
import subprocess
import threading
import psycopg2
import datetime
import os
import redis

repo_name = "ls-prime"
marklogic_path = "ls-prime/marklogic"
branch_name = "develop"

app = Flask(__name__)
CORS(app)

# Redis configuration
REDIS_HOST = 'redis.agentic-ai.lifesciences-dev.casinternal'
REDIS_PORT = 6379
REDIS_DB = 0
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)

# Database configuration (use environment variables for security)
DB_HOST = os.getenv('DB_HOST', 'postgres.agentic-ai.lifesciences-dev.casinternal')
DB_NAME = os.getenv('DB_NAME', 'deploymentdashboard')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASS = os.getenv('DB_PASS', 'postgres')

def create_tables():
    """Create tables for deployment history if they don't exist."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deploy_fr_history (
            build_id INTEGER PRIMARY KEY,
            deploy_datetime TIMESTAMP,
            output_log TEXT,
            status BOOLEAN,
            fr_version VARCHAR(50),
            structure_search_version VARCHAR(50)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deploy_ml_history (
            build_id INTEGER PRIMARY KEY,
            deploy_datetime TIMESTAMP,
            output_log TEXT,
            status BOOLEAN
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_next_fr_build_id():
    """Get the next build ID for frontend deployments."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("SELECT MAX(build_id) FROM deploy_fr_history")
    max_id = cur.fetchone()[0]
    cur.close()
    conn.close()
    return 1 if max_id is None else max_id + 1

def get_next_ml_build_id():
    """Get the next build ID for MarkLogic deployments."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("SELECT MAX(build_id) FROM deploy_ml_history")
    max_id = cur.fetchone()[0]
    cur.close()
    conn.close()
    return 1 if max_id is None else max_id + 1

def insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version):
    """Insert frontend deployment log into the database."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("INSERT INTO deploy_fr_history (build_id, deploy_datetime, output_log, status, fr_version, structure_search_version) VALUES (%s, %s, %s, %s, %s, %s)", 
                (build_id, dt, log, status, fr_version, structure_search_version))
    conn.commit()
    cur.close()
    conn.close()

def insert_ml_log(build_id, dt, log, status):
    """Insert MarkLogic deployment log into the database."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("INSERT INTO deploy_ml_history (build_id, deploy_datetime, output_log, status) VALUES (%s, %s, %s, %s)", 
                (build_id, dt, log, status))
    conn.commit()
    cur.close()
    conn.close()

def run_command(command):
    """Run a command and yield output line by line."""
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        for line in iter(process.stdout.readline, ''):
            yield f"{line}\n", None

        process.stdout.close()
        return_code = process.wait()

        yield None, return_code

    except Exception as e:
        yield f"❌ Error executing command: {str(e)}\n\n", None

@app.route('/')
def home():
    """Return API documentation."""
    return jsonify({
        "message": "Documentation",
        "endpoints": {
            "/": "Displays this doc",
            "/api/v1/deploy/fr": "Deploy UI and MIDDLEWARE in DEV-FULL",
            "/api/v1/deploy/ml": "Deploy MARKLOGIC in DEV-FULL",
            "/api/v1/history/fr": "Get history for FR deployments (last 10 or specific buildId)",
            "/api/v1/history/ml": "Get history for ML deployments (last 10 or specific buildId)",
            "/health": "Health check"
        },
        "guide": {
            "deploy_ui_and_middleware": "/api/v1/deploy/fr?fr_version=x.y.z-SNAPSHOT&structure_search_version=x.y.z-SNAPSHOT",
            "deploy_marklogic": "/api/v1/deploy/ml",
            "history_fr": "/api/v1/history/fr or /api/v1/history/fr?buildId=1234",
            "history_ml": "/api/v1/history/ml or /api/v1/history/ml?buildId=1234"
        }
    })

@app.route('/api/v1/deploy/fr')
def deploy_frontend():
    """Deploy UI and Middleware with provided versions using the deployment script."""
    fr_version = request.args.get('fr-version')
    structure_search_version = request.args.get('structure-search-version')
    params = 0
    if fr_version is None:
        params = 1
    if structure_search_version is None:
        params = 2
    if fr_version is None and structure_search_version is None:
        params = 3
    params_def = {
        1: "fr-version is missing in the query string",
        2: "structure-search-version is missing in the query string",
        3: "fr-version and structure-search-version is missing in the query string"
    }
    if params != 0:
        error_response = jsonify({
            "success": "false",
            "error": "Missing required parameter",
            "message": params_def[params],
            "required_parameters": ["fr-version", "structure-search-version"],
            "status_code": 400
        })
        return error_response

    # Check and acquire lock
    lock_key = 'deploy_fr_lock'
    if not r.set(lock_key, 'locked', nx=True, ex=3600):  # 1 hour timeout
        return jsonify({"error": "Deployment is going on for the frontend application."}), 409

    def generate():
        try:
            build_id = get_next_fr_build_id()
            dt = datetime.datetime.now()
            log = ''
            status = True

            msg = "Proceeding to deploy FRONTEND in DEV-FULL\n\n"
            yield msg
            log += msg
            sleep(1)

            msg = "UI VERSION --> {}\n".format(fr_version)
            yield msg
            log += msg
            sleep(1)

            msg = "MIDDLEWARE VERSION --> {}\n".format(fr_version)
            yield msg
            log += msg
            sleep(1)

            msg = "STRUCTURE SEARCH VERSION --> {}\n\n".format(structure_search_version)
            yield msg
            log += msg
            sleep(1)

            rc = None
            for output, return_code in run_command('./script.sh {} {}'.format(fr_version, structure_search_version)):
                if return_code is None:
                    yield output
                    log += output
                else:
                    rc = return_code

            if rc == 0:
                msg = "✅ Deployment Successful.\n\n"
                yield msg
                log += msg
            else:
                msg = f"❌ Deployment Failed {rc}\n\n"
                yield msg
                log += msg
                status = False

            insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version)
        finally:
            r.delete(lock_key)

    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/v1/deploy/ml')
def deploy_marklogic():
    """Deploy MarkLogic in DEV-FULL."""
    # Check and acquire lock
    lock_key = 'deploy_ml_lock'
    if not r.set(lock_key, 'locked', nx=True, ex=3600):  # 1 hour timeout
        return jsonify({"error": "Deployment is going on for the MarkLogic application."}), 409

    def generate():
        try:
            build_id = get_next_ml_build_id()
            dt = datetime.datetime.now()
            log = ''
            status = True

            msg = "Proceeding to deploy MARKLOGIC in DEV-FULL\n\n"
            yield msg
            log += msg
            sleep(1)

            msg = "Changing current directory to {} and checking out to {} branch\n".format(repo_name, branch_name)
            yield msg
            log += msg

            rc = None
            for output, return_code in run_command('cd $(pwd)/{}/ && git checkout {}'.format(repo_name, branch_name)):
                if return_code is None:
                    yield output
                    log += output
                else:
                    rc = return_code
                    if rc != 0:
                        status = False

            sleep(1)

            msg = "\nTaking pull from develop branch\n"
            yield msg
            log += msg

            rc = None
            for output, return_code in run_command('cd $(pwd)/{}/ && git pull'.format(repo_name, branch_name)):
                if return_code is None:
                    yield output
                    log += output
                else:
                    rc = return_code
                    if rc != 0:
                        status = False

            sleep(1)

            msg = "\nDeploying code\n"
            yield msg
            log += msg
            sleep(1)

            rc = None
            for output, return_code in run_command('cd $(pwd)/{} && ./gradlew mlDeploy -PenvironmentName=ls-dev-full-ml'.format(marklogic_path)):
                if return_code is None:
                    yield output
                    log += output
                else:
                    rc = return_code
                    if rc != 0:
                        status = False

            msg = "\nReloading modules\n"
            yield msg
            log += msg
            sleep(1)

            rc = None
            for output, return_code in run_command('cd $(pwd)/{} && ./gradlew mlDeploy -PenvironmentName=ls-dev-full-ml'.format(marklogic_path)):
                if return_code is None:
                    yield output
                    log += output
                else:
                    rc = return_code
                    if rc != 0:
                        status = False

            if status:
                msg = "✅ Deployment Successful.\n\n"
                yield msg
                log += msg
            else:
                msg = f"❌ Deployment Failed {rc}\n\n"
                yield msg
                log += msg

            insert_ml_log(build_id, dt, log, status)
        finally:
            r.delete(lock_key)

    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/v1/history/fr')
def history_fr():
    """Get frontend deployment history (last 10 or specific buildId)."""
    build_id = request.args.get('buildId')
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    if build_id:
        try:
            build_id = int(build_id)  # Convert to integer for query
            cur.execute("SELECT deploy_datetime, output_log, status, fr_version, structure_search_version FROM deploy_fr_history WHERE build_id = %s", (build_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return jsonify({
                    'buildId': str(build_id),
                    'datetime': row[0].isoformat(),
                    'output_log': row[1],
                    'status': row[2],
                    'fr-version': row[3],
                    'structure-search-version': row[4]
                })
            else:
                return jsonify({'error': 'Build ID not found'}), 404
        except ValueError:
            cur.close()
            conn.close()
            return jsonify({'error': 'Build ID must be an integer'}), 400
    else:
        cur.execute("SELECT build_id, deploy_datetime, status, fr_version, structure_search_version FROM deploy_fr_history ORDER BY deploy_datetime DESC LIMIT 10")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        history = [{'buildId': str(row[0]), 'datetime': row[1].isoformat(), 'status': row[2], 'fr-version': row[3], 'structure-search-version': row[4]} for row in rows]
        return jsonify(history)

@app.route('/api/v1/history/ml')
def history_ml():
    """Get MarkLogic deployment history (last 10 or specific buildId)."""
    build_id = request.args.get('buildId')
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    if build_id:
        try:
            build_id = int(build_id)  # Convert to integer for query
            cur.execute("SELECT deploy_datetime, output_log, status FROM deploy_ml_history WHERE build_id = %s", (build_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return jsonify({
                    'buildId': str(build_id),
                    'datetime': row[0].isoformat(),
                    'output_log': row[1],
                    'status': row[2]
                })
            else:
                return jsonify({'error': 'Build ID not found'}), 404
        except ValueError:
            cur.close()
            conn.close()
            return jsonify({'error': 'Build ID must be an integer'}), 400
    else:
        cur.execute("SELECT build_id, deploy_datetime, status FROM deploy_ml_history ORDER BY deploy_datetime DESC LIMIT 10")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        history = [{'buildId': str(row[0]), 'datetime': row[1].isoformat(), 'status': row[2]} for row in rows]
        return jsonify(history)

@app.route('/api/v1/health')
def health_check():
    """Check the health of the API service."""
    return jsonify({"status": "healthy", "service": "simple-command-api"})

if __name__ == '__main__':
    create_tables()  # Initialize database tables
    app.run(host='0.0.0.0', port=8081, debug=False)
