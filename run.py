import json
import time
from datetime import datetime, timedelta
from io import BytesIO
from os import environ
from zipfile import ZipFile
from flask_cors import CORS

import requests
from flask import Flask, Response, request
from pymongo import ASCENDING, MongoClient

# config system
app = Flask(__name__)
CORS(app, support_credentials=True)

# app.config.update(dict(SECRET_KEY=str(environ["DATABASE_PASS"])))
client = MongoClient(str(environ["DATABASE"]), username=environ["DATABASE_USER"], password=environ["DATABASE_PASS"])

db = client[environ["DATABASE_DB"]]

Headers = {
    "Accept": "application/vnd.github.everest-preview+json",
    "Authorization": "token " + str(environ["GITHUB_PAT"]),
}
repo = str(environ["REPO"])


def clear_queue():
    for element in db.tasks.find({"status": "in_progress"}):
        current_time = datetime.now()
        forward_time = datetime.fromtimestamp(element.get("created_at")) + timedelta(seconds=30)
        if current_time >= forward_time:
            check_suite = element.get("check_suite_id")
            response = requests.get(
                f"https://api.github.com/repos/{repo}/actions/runs?event=repository_dispatch&check_suite_id={check_suite}",
                headers=Headers,
            )
            run = json.loads(response.text)["workflow_runs"][0]
            if run["status"] == "completed" or run["status"] == "failure":
                db.tasks.update_one({"task_id": element.get("task_id")}, {"$set": {"status": run["status"]}})

    for element in db.tasks.find({"status": "waiting"}):
        current_time = datetime.now()
        forward_time = datetime.fromtimestamp(element.get("created_at")) + timedelta(seconds=200)
        if current_time >= forward_time:
            db.tasks.delete_one(element)


@app.route("/generate", methods=["POST"])
def post():
    # Expects Task_ID and post_body
    if not request.json.get("task_id"):
        return "Record not found", 400
    id = request.json.get("task_id")
    clear_queue()
    if db.tasks.count_documents({"status": "in_progress"}) >= 5:
        if not db.tasks.find_one({"task_id": id}):
            db.tasks.insert_one({"task_id": id, "status": "waiting", "created_at": int(datetime.now().timestamp())})
        return "Multiple Seeds are being generated, please wait", 202
    lowest = db.tasks.find_one({"status": "waiting"}, sort=[("created_at", ASCENDING)])
    if lowest and lowest.get("task_id", None) != id and db.tasks.count_documents({"status": "in_progress"}) >= 3:
        return "Multiple Seeds are being generated, please wait", 202
    if not db.tasks.find_one({"task_id": id}):
        body = {
            "event_type": f"generate_seed-{id}",
            "client_payload": {
                "id": id,
                "branch": str(request.json.get("branch")),
                "post_body": str(request.json.get("post_body")),
            },
        }
        requests.post(f"https://api.github.com/repos/{repo}/dispatches", headers=Headers, json=body)
        check_suite = None
        tries = 0
        while check_suite is None and tries < 10:
            time.sleep(1)
            response = requests.get(f"https://api.github.com/repos/{repo}/actions/runs?event=repository_dispatch", headers=Headers)
            for run in json.loads(response.text)["workflow_runs"]:
                if run.get("display_title") == f"generate_seed-{id}":
                    check_suite = run.get("check_suite_id")
            tries = tries + 1
        if not check_suite:
            return "Build Start failed", 400
        else:
            db.tasks.insert_one({"task_id": id, "check_suite_id": check_suite, "status": "in_progress", "created_at": int(datetime.now().timestamp())})
            return "Build Started", 201
    return "Invalid Request", 405


@app.route("/generate", methods=["GET"])
def get():
    # Expects task_id
    if not request.args.get("task_id"):
        return "Record not found", 400
    id = request.args.get("task_id")
    clear_queue()
    if not db.tasks.find_one({"task_id": id}):
        return "You must send a request to start a job first", 400
    else:
        found = db.tasks.find_one({"task_id": id})
        last_check = found.get("last_checked")
        current_time = datetime.now()
        forward_time = current_time + timedelta(seconds=10)
        if last_check == None or datetime.fromtimestamp(last_check) <= forward_time:
            check_suite = found.get("check_suite_id")
            response = requests.get(
                f"https://api.github.com/repos/{repo}/actions/runs?event=repository_dispatch&check_suite_id={check_suite}",
                headers=Headers,
            )
            run = json.loads(response.text)["workflow_runs"][0]
            if run["status"] == "completed" or run["status"] == "failure":
                artifacts_url = run["artifacts_url"]
                response = requests.get(artifacts_url, headers=Headers)
                response_body = json.loads(response.text)
                if response_body.get("total_count", 0) == 0:
                    db.tasks.delete_many({"task_id": id})
                    # requests.delete(run.get("url"), headers=Headers)
                    return "Something Went Wrong", 400
                else:
                    response = requests.get(response_body.get("artifacts")[0].get("archive_download_url"), headers=Headers)
                    requests.delete(run.get("url"), headers=Headers)
                    with ZipFile(BytesIO(response.content)) as thezip:
                        for zipinfo in thezip.infolist():
                            thefile = thezip.open(zipinfo)
                            db.tasks.delete_many({"task_id": id})
                            return Response(thefile, mimetype="text/plain", direct_passthrough=True)
            else:
                db.tasks.update_one({"task_id": id}, {"$set": {"last_checked": int(datetime.now().timestamp())}})

        return "Pending", 425


if __name__ == "__main__":
    app.run(debug=True)
