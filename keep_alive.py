from flask import Flask
from threading import Thread

app = Flask("")

@app.route("/")
def home():
    return "Bot is alive", 200

def run():
    app.run(host="0.0.0.0", port=3000)

def keep_alive():
    Thread(target=run, daemon=True).start()
