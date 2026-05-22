from flask import Flask, jsonify, render_template, request, send_file

from scraper import get_zip_file, reveal_folder, save_cookies, scrape_images

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/cookies", methods=["POST"])
def cookies_route():
    data = request.get_json(silent=True) or {}
    cookies_json = data.get("cookies", "")
    if not cookies_json.strip():
        return jsonify({"ok": False, "error": "Cookie JSON is required."}), 400

    result = save_cookies(cookies_json)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/scrape", methods=["POST"])
def scrape_route():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"ok": False, "error": "URL is required."}), 400

    try:
        result = scrape_images(url)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Scrape failed: {exc}"}), 500

    status = 200 if result.get("ok") else 422
    return jsonify(result), status


@app.route("/download/<folder_id>")
def download_route(folder_id):
    try:
        zip_path = get_zip_file(folder_id)
        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f"{folder_id.removesuffix('.zip')}.zip",
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@app.route("/reveal/<folder_id>")
def reveal_route(folder_id):
    try:
        result = reveal_folder(folder_id)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5001)
