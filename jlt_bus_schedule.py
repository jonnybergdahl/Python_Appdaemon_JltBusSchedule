from datetime import datetime, timedelta
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import appdaemon.plugins.hass.hassapi as hass

# Stops to query: (stop_id, destination, direction)
TARGETS = [
            (6001350, "Huskvarna via Centrum-Elmia", "north"),
            (6001353, "Jönköping Rådhusparken",       "north"),
          ]
# "too_late" threshold
TOO_LATE_TRESHOLD = 8
# Number of coming departures to handle
NUMBER_OF_DEPARTURES = 6


class JltBusSchedule(hass.Hass):
    def initialize(self):
        self.log("initialize called")
        # Config
        self.interval_seconds = 60     # how often to try again
        self.max_departures   = NUMBER_OF_DEPARTURES      # create at most 9 per suffix
        self.threshold_min    = TOO_LATE_TRESHOLD

        self.targets = TARGETS

        # HTTP session w/ retries + timeouts
        self.session = requests.Session()
        retry = Retry(
            total=2, connect=2, read=2, backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"])
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": "AppDaemon-JLT/1.0 (+homeassistant)"
        })

        self._busy = False
        # Start the self-scheduling loop
        try:
            # try a 1-second delay (some setups ignore 0)
            self.handle = self.run_in(self._tick, 1)
            self.log(f"_tick scheduled, handle={self.handle}", level="WARNING")
        except Exception as e:
            self.log(f"FAILED to schedule _tick: {e}", level="ERROR")

    def _tick(self, kwargs):
        self.log("_tick called")
        if self._busy:
            self.log("Previous run still in progress; skipping this tick.", level="WARNING")
            # Try again next interval, without queuing back-to-back
            self.run_in(self._tick, self.interval_seconds)
            return

        start = datetime.now()
        self._busy = True
        try:
            for stop_id, destination, suffix in self.targets:
                self.log(f"stop_id: {stop_id}")
                schedules = self.get_bus_schedules(stop_id, destination, self.max_departures)
                self.update_sensors(schedules, suffix)
        except Exception as e:
            self.log(f"Unhandled error: {e}", level="ERROR")
        finally:
            self._busy = False
            elapsed = (datetime.now() - start).total_seconds()
            # Schedule next run from "now" to avoid backlog
            self.run_in(self._tick, self.interval_seconds)
            self.log(f"Run finished in {elapsed:.1f}s")

    def set_state_if_changed(self, entity_id, state, attributes=None):
        # Avoid hammering HA if nothing changed
        cur = self.get_state(entity_id, attribute="all")
        cur_state = cur.get("state") if cur else None
        cur_attrs = cur.get("attributes") if cur else None
        if state != cur_state or (attributes is not None and attributes != cur_attrs):
            self.set_state(entity_id, state=state, attributes=attributes)

    def update_sensors(self, schedules, suffix):
        for idx, schedule in enumerate(schedules, start=1):
            prefix = f"sensor.jlt_bus_{schedule['line_number']}_{suffix}"
            sensor_name = f"{prefix}_departure_{idx}"

            # Helpers for displays/automations
            self.set_state_if_changed(f"{sensor_name}_departure", schedule['departure_time'])
            self.set_state_if_changed(f"{sensor_name}_time_to_departure", schedule['time_to_departure'])
            self.set_state_if_changed(f"{sensor_name}_too_late", schedule['too_late'])

            attributes = {
                "line_number": schedule['line_number'],
                "direction": schedule['direction'],
                "time_to_departure": schedule['time_to_departure'],
                "too_late": schedule['too_late'],
            }
            self.log(f"{sensor_name}: {schedule['departure_time']} ({schedule['time_to_departure']}), "
                     f"line {schedule['line_number']}, dir: {schedule['direction']}")
            self.set_state_if_changed(sensor_name, schedule['departure_time'], attributes)

    def get_bus_schedules(self, stop_id, destination, max_items):
        url = f"https://www.jlt.se/api/StopAreaApi/GetClosestDepartures?fromId={stop_id}&take={NUMBER_OF_DEPARTURES}"
        self.log(url)
        try:
            # Fail fast if JLT is slow/unreachable
            resp = self.session.get(url, timeout=(3.0, 5.0))
            resp.raise_for_status()
        except Exception as e:
            self.log(f"Request failed for stop {stop_id}: {e}", level="ERROR")
            return []

        # lxml is faster; falls back to built-in if not available
        parser = "lxml"  # if lxml is installed in your env; otherwise use "html.parser"
        try:
            soup = BeautifulSoup(resp.content, parser)
        except Exception:
            soup = BeautifulSoup(resp.content, "html.parser")

        schedule_list = []
        travel_suggestions = soup.select("div.travel-suggestion")

        now = datetime.now()
        threshold = timedelta(minutes=self.threshold_min)

        for sug in travel_suggestions:
            direction_el = sug.select_one("div.direction")
            if not direction_el:
                continue

            # The JLT fragment often contains a prefix like "Mot " – be robust:
            direction_text = direction_el.get_text(strip=True)
            # Keep original text; we only need to check substring match
            if destination not in direction_text:
                continue

            line_el = sug.select_one("div.line-info span")
            time_el = sug.select_one("p")
            if not line_el or not time_el:
                continue

            line_number = line_el.get_text(strip=True)
            raw = time_el.get_text(strip=True)

            # Expect formats like "12:34 (5 min)" or "Har avgått"
            if "Har avgått" in raw:
                continue

            dep_time = raw[:5]            # "HH:MM"
            ttd = raw[5:].strip("() ")    # after the space

            try:
                target_time = datetime.strptime(dep_time, "%H:%M").replace(
                    year=now.year, month=now.month, day=now.day
                )
            except ValueError:
                # Skip unexpected formats
                continue

            too_late = now > (target_time - threshold)

            schedule_list.append({
                "line_number": line_number,
                "departure_time": dep_time,
                "direction": direction_text,
                "time_to_departure": ttd,
                "too_late": too_late
            })

            self.log(
                f"Found departure: line {line_number}, "
                f"direction '{direction_text}', "
                f"departure {dep_time}, "
                f"time_to_departure {ttd}, too_late={too_late}"
            )

            if len(schedule_list) >= max_items:
                break

        return schedule_list
