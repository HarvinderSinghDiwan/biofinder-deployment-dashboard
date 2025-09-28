from flask import Flask, Response, jsonify, request
from flask_cors import CORS
import subprocess
import threading
import psycopg2
import datetime
import os
import redis
import time
import uuid
import signal
import psutil
import queue

repo_name = "ls-prime"
marklogic_path = "ls-prime/marklogic"

app = Flask(__name__)
CORS(app)

# Redis configuration
REDIS_HOST = 'redis.agentic-ai.lifesciences-dev.casinternal'
REDIS_PORT = 6379
REDIS_DB = 0
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)

# Database configuration
DB_HOST = os.getenv('DB_HOST', 'postgres.agentic-ai.lifesciences-dev.casinternal')
DB_NAME = os.getenv('DB_NAME', 'deploymentdashboard')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASS = os.getenv('DB_PASS', 'postgres')

# Lock settings
LOCK_TIMEOUT = 300  # 5 minutes timeout
HEARTBEAT_INTERVAL = 0.1  # 100ms for immediate abort detection
SERVER_ID = str(uuid.uuid4())  # Unique server identifier

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
            structure_search_version VARCHAR(50),
            aborted BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deploy_ml_history (
            build_id INTEGER PRIMARY KEY,
            deploy_datetime TIMESTAMP,
            output_log TEXT,
            status BOOLEAN,
            branch_name VARCHAR(100),
            environment_type VARCHAR(100),
            aborted BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deploy_cj_history (
            build_id INTEGER PRIMARY KEY,
            deploy_datetime TIMESTAMP,
            output_log TEXT,
            status BOOLEAN,
            job_name VARCHAR(100),
            branch_name VARCHAR(100),
            environment_type VARCHAR(100),
            aborted BOOLEAN DEFAULT FALSE
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

def get_next_cj_build_id():
    """Get the next build ID for corb job deployments."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("SELECT MAX(build_id) FROM deploy_cj_history")
    max_id = cur.fetchone()[0]
    cur.close()
    conn.close()
    return 1 if max_id is None else max_id + 1

def insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, aborted=False):
    """Insert frontend deployment log into the database."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("INSERT INTO deploy_fr_history (build_id, deploy_datetime, output_log, status, fr_version, structure_search_version, aborted) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
                (build_id, dt, log, status, fr_version, structure_search_version, aborted))
    conn.commit()
    cur.close()
    conn.close()

def insert_ml_log(build_id, dt, log, status, branch_name, environment_type, aborted=False):
    """Insert MarkLogic deployment log into the database."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("INSERT INTO deploy_ml_history (build_id, deploy_datetime, output_log, status, branch_name, environment_type, aborted) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
                (build_id, dt, log, status, branch_name, environment_type, aborted))
    conn.commit()
    cur.close()
    conn.close()

def insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, aborted=False):
    """Insert corb job deployment log into the database."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    cur.execute("INSERT INTO deploy_cj_history (build_id, deploy_datetime, output_log, status, job_name, branch_name, environment_type, aborted) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", 
                (build_id, dt, log, status, job_name, branch_name, environment_type, aborted))
    conn.commit()
    cur.close()
    conn.close()

def getEnvironmentName(environment_type):
    if environment_type == 'DEV-FULL':
        return 'ls-dev-full-ml'
    if environment_type == 'DEV-SMALL':
        return 'ls-dev-small-ml'
    if environment_type == 'INGESTION':
        return 'ingestion'
    if environment_type == 'TEST':
        return 'test'

    

def terminate_process_tree(pid, sig=signal.SIGTERM):
    """Terminate a process and all its children using psutil."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            child.send_signal(sig)
        parent.send_signal(sig)
    except psutil.NoSuchProcess:
        pass

def run_command(command, abort_event, current_process_holder=None):
    """Run a command and yield output line by line, checking for abort."""
    try:
        # Set process group if on Unix, for compatibility
        def preexec_fn():
            if os.name != 'nt':
                os.setpgrp()

        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
            preexec_fn=preexec_fn if os.name != 'nt' else None
        )
        
        if current_process_holder is not None:
            current_process_holder[0] = process

        q = queue.Queue()
        
        def enqueue_output():
            for line in iter(process.stdout.readline, ''):
                q.put(line)
            process.stdout.close()
            q.put(None)  # Sentinel to indicate end

        t = threading.Thread(target=enqueue_output)
        t.daemon = True
        t.start()

        while True:
            if abort_event.is_set():
                try:
                    print(f"Aborting process {process.pid}")
                    terminate_process_tree(process.pid, signal.SIGINT)
                    time.sleep(1)
                    if process.poll() is None:
                        print(f"Process {process.pid} still running, sending SIGTERM")
                        terminate_process_tree(process.pid, signal.SIGTERM)
                        time.sleep(1)
                        if process.poll() is None:
                            print(f"Process {process.pid} still running, sending SIGKILL")
                            try:
                                parent = psutil.Process(process.pid)
                                children = parent.children(recursive=True)
                                for child in children:
                                    child.kill()
                                parent.kill()
                            except psutil.NoSuchProcess:
                                pass
                            time.sleep(0.5)
                            if process.poll() is None:
                                print(f"Warning: Process {process.pid} could not be terminated")
                except Exception as e:
                    print(f"Error during termination: {str(e)}")
                finally:
                    if current_process_holder is not None:
                        current_process_holder[0] = None
                    # Drain the queue to avoid leaving the thread hanging
                    while not q.empty():
                        q.get_nowait()
                    yield "❌ Process Aborted.\n\n", None
                    return

            try:
                line = q.get_nowait()
                if line is None:
                    break
                yield f"{line}\n", None
            except queue.Empty:
                if process.poll() is not None:
                    # Process ended, wait for remaining output
                    time.sleep(0.1)
                    continue
                time.sleep(0.05)  # Small sleep to avoid busy loop

        t.join(timeout=1.0)
        return_code = process.wait()

        if current_process_holder is not None:
            current_process_holder[0] = None

        yield None, return_code

    except Exception as e:
        if current_process_holder is not None:
            current_process_holder[0] = None
        yield f"❌ Error executing command: {str(e)}\n\n", None

def heartbeat(lock_key, stop_event, abort_key, abort_event):
    """Periodically refresh the lock's expiration time and check for abort signal."""
    while not stop_event.is_set():
        r.expire(lock_key, LOCK_TIMEOUT)
        if r.get(abort_key):
            abort_event.set()
            r.delete(abort_key)
        time.sleep(HEARTBEAT_INTERVAL)

def cleanup_stale_locks():
    """Check and clean up stale locks and abort keys on server startup."""
    for lock_key in ['deploy_fr_lock', 'deploy_ml_cj_lock']:
        if r.exists(lock_key):
            if r.ttl(lock_key) <= 0:
                r.delete(lock_key)
    for abort_key in ['abort_fr', 'abort_ml', 'abort_cj']:
        if r.exists(abort_key):
            r.delete(abort_key)

@app.route('/')
def home():
    """Return API documentation."""
    return jsonify({
        "message": "Documentation",
        "endpoints": {
            "/": "Displays this doc",
            "/api/v1/deploy/fr": "Deploy UI and MIDDLEWARE in DEV-FULL",
            "/api/v1/deploy/ml": "Deploy MARKLOGIC with specified branch and environment",
            "/api/v1/run/cj": "Run corb job with specified job name, branch, and environment",
            "/api/v1/history/fr": "Get history for FR deployments (last 10 or specific buildId)",
            "/api/v1/history/ml": "Get history for ML deployments (last 10 or specific buildId)",
            "/api/v1/history/cj": "Get history for corb job runs (last 10 or specific buildId)",
            "/api/v1/abort/fr": "Abort ongoing frontend deployment",
            "/api/v1/abort/ml": "Abort ongoing MarkLogic deployment",
            "/api/v1/abort/cj": "Abort ongoing corb job run",
            "/health": "Health check"
        },
        "guide": {
            "deploy_ui_and_middleware": "/api/v1/deploy/fr?fr_version=x.y.z-SNAPSHOT&structure_search_version=x.y.z-SNAPSHOT",
            "deploy_marklogic": "/api/v1/deploy/ml?branchName=develop&environmentType=ls-dev-full-ml",
            "run_corb_job": "/api/v1/run/cj?job-name=somename&branchName=develop&environmentType=ls-dev-full-ml",
            "history_fr": "/api/v1/history/fr or /api/v1/history/fr?buildId=1234",
            "history_ml": "/api/v1/history/ml or /api/v1/history/ml?buildId=1234",
            "history_cj": "/api/v1/history/cj or /api/v1/history/cj?buildId=1234"
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
        return jsonify({
            "success": "false",
            "error": "Missing required parameter",
            "message": params_def[params],
            "required_parameters": ["fr-version", "structure-search-version"],
            "status_code": 400
        }), 400

    lock_key = 'deploy_fr_lock'
    if not r.set(lock_key, SERVER_ID, nx=True, ex=LOCK_TIMEOUT):
        return jsonify({"error": "Deployment is going on for the frontend application."}), 409

    stop_heartbeat = threading.Event()
    abort_event = threading.Event()
    current_process_holder = [None]
    abort_key = 'abort_fr'
    heartbeat_thread = threading.Thread(target=heartbeat, args=(lock_key, stop_heartbeat, abort_key, abort_event))
    heartbeat_thread.start()

    def generate():
        try:
            build_id = get_next_fr_build_id()
            dt = datetime.datetime.now()
            log = ''
            status = True

            if abort_event.is_set():
                status = False
                msg = "❌ Deployment Aborted.\n\n"
                yield msg
                log += msg
                insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, True)
                return

            msg = "Proceeding to deploy FRONTEND in DEV-FULL\n\n"
            yield msg
            log += msg

            if abort_event.is_set():
                status = False
                msg = "❌ Deployment Aborted.\n\n"
                yield msg
                log += msg
                insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, True)
                return

            msg = f"UI VERSION --> {fr_version}\n"
            yield msg
            log += msg

            if abort_event.is_set():
                status = False
                msg = "❌ Deployment Aborted.\n\n"
                yield msg
                log += msg
                insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, True)
                return

            msg = f"MIDDLEWARE VERSION --> {fr_version}\n"
            yield msg
            log += msg

            if abort_event.is_set():
                status = False
                msg = "❌ Deployment Aborted.\n\n"
                yield msg
                log += msg
                insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, True)
                return

            msg = f"STRUCTURE SEARCH VERSION --> {structure_search_version}\n\n"
            yield msg
            log += msg

            if abort_event.is_set():
                status = False
                msg = "❌ Deployment Aborted.\n\n"
                yield msg
                log += msg
                insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, True)
                return

            for output, return_code in run_command(f'./script.sh {fr_version} {structure_search_version}', abort_event, current_process_holder):
                if abort_event.is_set():
                    status = False
                    msg = "❌ Deployment Aborted.\n\n"
                    yield msg
                    log += msg
                    insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, True)
                    return
                if return_code is None:
                    if output:
                        yield output
                        log += output
                else:
                    if return_code != 0:
                        status = False
                        msg = f"❌ Deployment Failed {return_code}\n\n"
                        yield msg
                        log += msg
                        insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, False)
                        return

            msg = "✅ Deployment Successful.\n\n"
            yield msg
            log += msg
            insert_fr_log(build_id, dt, log, status, fr_version, structure_search_version, False)

        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=0.5)
            if r.get(lock_key) == SERVER_ID.encode():
                r.delete(lock_key)

    return Response(generate(), mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})

@app.route('/api/v1/deploy/ml')
def deploy_marklogic():
    """Deploy MarkLogic with specified branch and environment."""
    branch_name = request.args.get('branchName')
    environment_type = request.args.get('environmentType')
    params = 0
    if branch_name is None:
        params = 1
    if environment_type is None:
        params = 2
    if branch_name is None and environment_type is None:
        params = 3
    params_def = {
        1: "branchName is missing in the query string",
        2: "environmentType is missing in the query string",
        3: "branchName and environmentType are missing in the query string"
    }
    if params != 0:
        return jsonify({
            "success": "false",
            "error": "Missing required parameter",
            "message": params_def[params],
            "required_parameters": ["branchName", "environmentType"],
            "status_code": 400
        }), 400

    EnvironmentName = getEnvironmentName(environment_type)
    
    lock_key = 'deploy_ml_cj_lock'
    if not r.set(lock_key, SERVER_ID, nx=True, ex=LOCK_TIMEOUT):
        return jsonify({"error": "Deployment is going on for the MarkLogic application or a corb job is running."}), 409
    
    stop_heartbeat = threading.Event()
    abort_event = threading.Event()
    current_process_holder = [None]
    abort_key = 'abort_ml'
    heartbeat_thread = threading.Thread(target=heartbeat, args=(lock_key, stop_heartbeat, abort_key, abort_event))
    heartbeat_thread.start()

    def generate():
        try:
            build_id = get_next_ml_build_id()
            dt = datetime.datetime.now()
            log = ''
            status = True

            if abort_event.is_set():
                status = False
                msg = "❌ Deployment Aborted.\n\n"
                yield msg
                log += msg
                insert_ml_log(build_id, dt, log, status, branch_name, environment_type, True)
                return

            msg = f"Proceeding to deploy MARKLOGIC in {environment_type}\n\n"
            yield msg
            log += msg

            if abort_event.is_set():
                status = False
                msg = "❌ Deployment Aborted.\n\n"
                yield msg
                log += msg
                insert_ml_log(build_id, dt, log, status, branch_name, environment_type, True)
                return

            msg = f"Changing current directory to {repo_name} and checking out to {branch_name} branch\n"
            yield msg
            log += msg

            for output, return_code in run_command(f'cd $(pwd)/{repo_name}/ && git checkout {branch_name}', abort_event, current_process_holder):
                if abort_event.is_set():
                    status = False
                    msg = "❌ Deployment Aborted.\n\n"
                    yield msg
                    log += msg
                    insert_ml_log(build_id, dt, log, status, branch_name, environment_type, True)
                    return
                if return_code is None:
                    if output:
                        yield output
                        log += output
                else:
                    if return_code != 0:
                        status = False
                        msg = f"❌ Deployment Failed {return_code}\n\n"
                        yield msg
                        log += msg
                        insert_ml_log(build_id, dt, log, status, branch_name, environment_type, False)
                        return

            msg = f"\nTaking pull from {branch_name} branch\n"
            yield msg
            log += msg

            for output, return_code in run_command(f'cd $(pwd)/{repo_name}/ && git pull', abort_event, current_process_holder):
                if abort_event.is_set():
                    status = False
                    msg = "❌ Deployment Aborted.\n\n"
                    yield msg
                    log += msg
                    insert_ml_log(build_id, dt, log, status, branch_name, environment_type, True)
                    return
                if return_code is None:
                    if output:
                        yield output
                        log += output
                else:
                    if return_code != 0:
                        status = False
                        msg = f"❌ Deployment Failed {return_code}\n\n"
                        yield msg
                        log += msg
                        insert_ml_log(build_id, dt, log, status, branch_name, environment_type, False)
                        return

            msg = "\nDeploying code\n"
            yield msg
            log += msg

            for output, return_code in run_command(f'cd $(pwd)/{marklogic_path} && ./gradlew mlDeploy -PenvironmentName={EnvironmentName}', abort_event, current_process_holder):
                if abort_event.is_set():
                    status = False
                    msg = "❌ Deployment Aborted.\n\n"
                    yield msg
                    log += msg
                    insert_ml_log(build_id, dt, log, status, branch_name, environment_type, True)
                    return
                if return_code is None:
                    if output:
                        yield output
                        log += output
                else:
                    if return_code != 0:
                        status = False
                        msg = f"❌ Deployment Failed {return_code}\n\n"
                        yield msg
                        log += msg
                        insert_ml_log(build_id, dt, log, status, branch_name, environment_type, False)
                        return

            msg = "\nReloading modules\n"
            yield msg
            log += msg

            for output, return_code in run_command(f'cd $(pwd)/{marklogic_path} && ./gradlew mlDeploy -PenvironmentName={EnvironmentName}', abort_event, current_process_holder):
                if abort_event.is_set():
                    status = False
                    msg = "❌ Deployment Aborted.\n\n"
                    yield msg
                    log += msg
                    insert_ml_log(build_id, dt, log, status, branch_name, environment_type, True)
                    return
                if return_code is None:
                    if output:
                        yield output
                        log += output
                else:
                    if return_code != 0:
                        status = False
                        msg = f"❌ Deployment Failed {return_code}\n\n"
                        yield msg
                        log += msg
                        insert_ml_log(build_id, dt, log, status, branch_name, environment_type, False)
                        return

            msg = "✅ Deployment Successful.\n\n"
            yield msg
            log += msg
            insert_ml_log(build_id, dt, log, status, branch_name, environment_type, False)

        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=0.5)
            if r.get(lock_key) == SERVER_ID.encode():
                r.delete(lock_key)

    return Response(generate(), mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})

@app.route('/api/v1/run/cj')
def run_corb_job():
    """Run corb job with specified job name, branch, and environment."""
    job_name = request.args.get('job-name')
    branch_name = request.args.get('branchName')
    environment_type = request.args.get('environmentType')
    params = 0
    if job_name is None:
        params = 1
    if branch_name is None:
        params = 2
    if environment_type is None:
        params = 3
    if job_name is None and branch_name is None:
        params = 4
    if job_name is None and environment_type is None:
        params = 5
    if branch_name is None and environment_type is None:
        params = 6
    if job_name is None and branch_name is None and environment_type is None:
        params = 7
    params_def = {
        1: "job-name is missing in the query string",
        2: "branchName is missing in the query string",
        3: "environmentType is missing in the query string",
        4: "job-name and branchName are missing in the query string",
        5: "job-name and environmentType are missing in the query string",
        6: "branchName and environmentType are missing in the query string",
        7: "job-name, branchName, and environmentType are missing in the query string"
    }
    if params != 0:
        return jsonify({
            "success": "false",
            "error": "Missing required parameter",
            "message": params_def[params],
            "required_parameters": ["job-name", "branchName", "environmentType"],
            "status_code": 400
        }), 400
    EnvironmentName = getEnvironmentName(environment_type)
    lock_key = 'deploy_ml_cj_lock'
    if not r.set(lock_key, SERVER_ID, nx=True, ex=LOCK_TIMEOUT):
        return jsonify({"error": "corb job is already running or a MarkLogic deployment is in progress."}), 409

    stop_heartbeat = threading.Event()
    abort_event = threading.Event()
    current_process_holder = [None]
    abort_key = 'abort_cj'
    heartbeat_thread = threading.Thread(target=heartbeat, args=(lock_key, stop_heartbeat, abort_key, abort_event))
    heartbeat_thread.start()

    def generate():
        try:
            build_id = get_next_cj_build_id()
            dt = datetime.datetime.now()
            log = ''
            status = True

            if abort_event.is_set():
                status = False
                msg = "❌ Job Run Aborted.\n\n"
                yield msg
                log += msg
                insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, True)
                return

            msg = f"Proceeding to run corb job {job_name} in {environment_type}\n\n"
            yield msg
            log += msg

            if abort_event.is_set():
                status = False
                msg = "❌ Job Run Aborted.\n\n"
                yield msg
                log += msg
                insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, True)
                return

            msg = f"Changing current directory to {repo_name} and checking out to {branch_name} branch\n"
            yield msg
            log += msg

            for output, return_code in run_command(f'cd $(pwd)/{repo_name}/ && git checkout {branch_name}', abort_event, current_process_holder):
                if abort_event.is_set():
                    status = False
                    msg = "❌ Job Run Aborted.\n\n"
                    yield msg
                    log += msg
                    insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, True)
                    return
                if return_code is None:
                    if output:
                        yield output
                        log += output
                else:
                    if return_code != 0:
                        status = False
                        msg = f"❌ Job Run Failed {return_code}\n\n"
                        yield msg
                        log += msg
                        insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, False)
                        return

            msg = f"\nTaking pull from {branch_name} branch\n"
            yield msg
            log += msg

            for output, return_code in run_command(f'cd $(pwd)/{repo_name}/ && git pull', abort_event, current_process_holder):
                if abort_event.is_set():
                    status = False
                    msg = "❌ Job Run Aborted.\n\n"
                    yield msg
                    log += msg
                    insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, True)
                    return
                if return_code is None:
                    if output:
                        yield output
                        log += output
                else:
                    if return_code != 0:
                        status = False
                        msg = f"❌ Job Run Failed {return_code}\n\n"
                        yield msg
                        log += msg
                        insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, False)
                        return

            msg = "\nDeploying code\n"
            yield msg
            log += msg

            for output, return_code in run_command(f'cd $(pwd)/{marklogic_path} && ./gradlew {job_name} -PenvironmentName={EnvironmentName}', abort_event, current_process_holder):
                if abort_event.is_set():
                    status = False
                    msg = "❌ Job Run Aborted.\n\n"
                    yield msg
                    log += msg
                    insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, True)
                    return
                if return_code is None:
                    if output:
                        yield output
                        log += output
                else:
                    if return_code != 0:
                        status = False
                        msg = f"❌ Job Run Failed {return_code}\n\n"
                        yield msg
                        log += msg
                        insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, False)
                        return

            msg = "✅ Job Run Successful.\n\n"
            yield msg
            log += msg
            insert_cj_log(build_id, dt, log, status, job_name, branch_name, environment_type, False)

        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=0.5)
            if r.get(lock_key) == SERVER_ID.encode():
                r.delete(lock_key)

    return Response(generate(), mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})

@app.route('/api/v1/abort/fr')
def abort_frontend():
    lock_key = 'deploy_fr_lock'
    abort_key = 'abort_fr'
    if r.exists(lock_key):
        r.set(abort_key, 'true', ex=30)
        return jsonify({"message": "Abort signal sent for frontend deployment."})
    else:
        return jsonify({"error": "No ongoing frontend deployment to abort."}), 404

@app.route('/api/v1/abort/ml')
def abort_marklogic():
    lock_key = 'deploy_ml_cj_lock'
    abort_key = 'abort_ml'
    if r.exists(lock_key):
        r.set(abort_key, 'true', ex=30)
        return jsonify({"message": "Abort signal sent for MarkLogic deployment."})
    else:
        return jsonify({"error": "No ongoing MarkLogic deployment to abort."}), 404

@app.route('/api/v1/abort/cj')
def abort_corb_job():
    lock_key = 'deploy_ml_cj_lock'
    abort_key = 'abort_cj'
    if r.exists(lock_key):
        r.set(abort_key, 'true', ex=30)
        return jsonify({"message": "Abort signal sent for corb job run."})
    else:
        return jsonify({"error": "No ongoing corb job run to abort."}), 404

@app.route('/api/v1/history/fr')
def history_fr():
    """Get frontend deployment history (last 10 or specific buildId)."""
    build_id = request.args.get('buildId')
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    if build_id:
        try:
            build_id = int(build_id)
            cur.execute("SELECT deploy_datetime, output_log, status, fr_version, structure_search_version, aborted FROM deploy_fr_history WHERE build_id = %s", (build_id,))
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
                    'structure-search-version': row[4],
                    'aborted': row[5]
                })
            else:
                return jsonify({'error': 'Build ID not found'}), 404
        except ValueError:
            cur.close()
            conn.close()
            return jsonify({'error': 'Build ID must be an integer'}), 400
    else:
        cur.execute("SELECT build_id, deploy_datetime, status, fr_version, structure_search_version, aborted FROM deploy_fr_history ORDER BY deploy_datetime DESC LIMIT 10")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        history = [{'buildId': str(row[0]), 'datetime': row[1].isoformat(), 'status': row[2], 'fr-version': row[3], 'structure-search-version': row[4], 'aborted': row[5]} for row in rows]
        return jsonify(history)

@app.route('/api/v1/history/ml')
def history_ml():
    """Get MarkLogic deployment history (last 10 or specific buildId)."""
    build_id = request.args.get('buildId')
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    if build_id:
        try:
            build_id = int(build_id)
            cur.execute("SELECT deploy_datetime, output_log, status, branch_name, environment_type, aborted FROM deploy_ml_history WHERE build_id = %s", (build_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return jsonify({
                    'buildId': str(build_id),
                    'datetime': row[0].isoformat(),
                    'output_log': row[1],
                    'status': row[2],
                    'branchName': row[3],
                    'environmentType': row[4],
                    'aborted': row[5]
                })
            else:
                return jsonify({'error': 'Build ID not found'}), 404
        except ValueError:
            cur.close()
            conn.close()
            return jsonify({'error': 'Build ID must be an integer'}), 400
    else:
        cur.execute("SELECT build_id, deploy_datetime, status, branch_name, environment_type, aborted FROM deploy_ml_history ORDER BY deploy_datetime DESC LIMIT 10")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        history = [{'buildId': str(row[0]), 'datetime': row[1].isoformat(), 'status': row[2], 'branchName': row[3], 'environmentType': row[4], 'aborted': row[5]} for row in rows]
        return jsonify(history)

@app.route('/api/v1/history/cj')
def history_cj():
    """Get corb job run history (last 10 or specific buildId)."""
    build_id = request.args.get('buildId')
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    if build_id:
        try:
            build_id = int(build_id)
            cur.execute("SELECT deploy_datetime, output_log, status, job_name, branch_name, environment_type, aborted FROM deploy_cj_history WHERE build_id = %s", (build_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return jsonify({
                    'buildId': str(build_id),
                    'datetime': row[0].isoformat(),
                    'output_log': row[1],
                    'status': row[2],
                    'job-name': row[3],
                    'branchName': row[4],
                    'environmentType': row[5],
                    'aborted': row[6]
                })
            else:
                return jsonify({'error': 'Build ID not found'}), 404
        except ValueError:
            cur.close()
            conn.close()
            return jsonify({'error': 'Build ID must be an integer'}), 400
    else:
        cur.execute("SELECT build_id, deploy_datetime, status, job_name, branch_name, environment_type, aborted FROM deploy_cj_history ORDER BY deploy_datetime DESC LIMIT 10")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        history = [{'buildId': str(row[0]), 'datetime': row[1].isoformat(), 'status': row[2], 'jobName': row[3], 'branchName': row[4], 'environmentType': row[5], 'aborted': row[6]} for row in rows]
        return jsonify(history)

@app.route('/api/v1/health')
def health_check():
    """Check the health of the API service."""
    return jsonify({"status": "healthy", "service": "simple-command-api"})

if __name__ == '__main__':
    cleanup_stale_locks()
    create_tables()
    app.run(host='0.0.0.0', port=8080, debug=False)
