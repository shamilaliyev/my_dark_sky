from flask import Flask, render_template, request
from datetime import date, datetime

app = Flask(__name__)

DEFAULT_CITY = "Baku"


@app.route("/")
def index():
    city = request.args.get("city", "").strip()
    latitude = request.args.get("lat", "").strip()
    longitude = request.args.get("lon", "").strip()
    selected_date = request.args.get("date", date.today().isoformat())

    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        selected_date = date.today().isoformat()

    return render_template(
        "index.html",
        city=city or (DEFAULT_CITY if not latitude and not longitude else ""),
        selected_date=selected_date,
        lat=latitude,
        lon=longitude
    )


if __name__ == "__main__":
    app.run(debug=True)
