import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["https://onebassplayer.github.io", "https://moonshotexitplanner.com", "https://moonshotfitanalyzer.com"])
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

@app.route("/v1/messages", methods=["POST"])
def proxy():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "API key not configured"}), 500

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        },
        json=request.get_json()
    )
    return jsonify(response.json()), response.status_code

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
