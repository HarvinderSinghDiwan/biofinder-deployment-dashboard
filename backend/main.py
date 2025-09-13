from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from time import sleep
import subprocess
import threading

repo_name = "ls-prime"
marklogic_path="ls-prime/marklogic"
branch_name="develop"

app = Flask(__name__)
CORS(app)
def run_command(command):
    """Run a command and yield output line by line"""
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
        yield f"❌ Error executing command: {str(e)}\n\n"

@app.route('/')
def home():
    return jsonify({
        "message": "Documentation",
        "endpoints": {
            "/": "Displays this doc",
            "/api/v1/deploy/fr": "Deploy UI and MIDDLEWARE in DEV-FULL",
            "/api/v1/deploy/ml": "Deploy MARKLOGIC in DEV-FULL",
            "/health": "Health check"
        },
        "guide": {
            "deploy_ui_and_middleware": "/api/v1/deploy/fr?fr_version=x.y.z-SNAPSHOT&structure_search_version=x.y.z-SNAPSHOT",
            "deploy_marklogic": "/api/v1/deploy/ml"
        }
    })

@app.route('/api/v1/deploy/fr')
def deploy_frontend():
    """Deploy UI and Middelware with provided versions using the deployment script."""
    fr_version = request.args.get('fr-version')
    structure_search_version = request.args.get('structure-search-version')
    params = 0
    if fr_version is None:
        params = 1
    if structure_search_version is None:
        params = 2
    if fr_version is None and structure_search_version is None:
        params = 3
    params_def = {1:"fr-version is missing in the query string",2:"structure-search-version is missing in the query string",3:"fr-version and structure-search-version is missing in the query string"}
    if params == 0:
        def generate():
            yield "Proceeding to deploy FRONTEND in DEV-FULL\n\n"
            sleep(1)
            yield "UI VERSION --> {}\n".format(fr_version)
            sleep(1)
            yield "MIDDLEWARE VERSION --> {}\n".format(fr_version)
            sleep(1)
            yield "STRUCTURE SEARCH VERSION --> {}\n\n".format(structure_search_version)
            sleep(1)
            for output, rc in run_command('./script.sh {} {}'.format(fr_version,structure_search_version)):
                if rc == None:
                    yield output
            if rc == 0:
                yield "✅ Deployment Successful.\n\n"
            else:
                yield f"❌ Deployment Failed {rc}\n\n"
        return Response(generate(), mimetype='text/event-stream')
    else:
        error_response = jsonify({
                    "success": "false",
                    "error": "Missing required parameter",
                    "message": params_def[params],
                    "required_parameters": ["fr-version","structure-search-version"],
                    "status_code": 400
                })
        return error_response

@app.route('/api/v1/deploy/ml')
def deploy_marklogic():
    def generate():
        yield "Proceeding to deploy MARKLOGIC in DEV-FULL\n\n"
        sleep(1)
        yield "Changing current directory to {} and checking out to {} branch\n".format(repo_name,branch_name)
        for output , rc in run_command('cd $(pwd)/{}/ && git checkout {}'.format(repo_name,branch_name)):
            if rc == None:
                yield output
        sleep(1)
        yield "\nTaking pull from develop branch\n"
        for output , rc in run_command('cd $(pwd)/{}/ && git pull'.format(repo_name,branch_name)):
            if rc == None:
                yield output
        sleep(1)
        yield "\nDeploying code\n"
        sleep(1)
        for output , rc  in run_command('cd $(pwd)/{} && ./gradlew mlDeploy -PenvironmentName=ls-dev-full-ml'.format(marklogic_path)):
            if rc == None:
                yield output
        yield "\nReloading modules\n"
        sleep(1)
        for output , rc  in run_command('cd $(pwd)/{} && ./gradlew mlDeploy -PenvironmentName=ls-dev-full-ml'.format(marklogic_path)):
            if rc == None:
                yield output
        if rc == 0:
            yield "✅ Deployment Successful.\n\n"
        else:
            yield output
            yield f"❌ Deployment Failed {rc}\n\n"
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/v1/health')
def health_check():
    return jsonify({"status": "healthy", "service": "simple-command-api"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)

