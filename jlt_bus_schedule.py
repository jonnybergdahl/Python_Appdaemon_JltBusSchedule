from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import appdaemon.plugins.hass.hassapi as hass
import re


class JltBusSchedule(hass.Hass):
    def initialize(self):
        # Schedule the function to run periodically, e.g., every 5 minutes
        self.run_every(self.set_bus_schedule_sensors, "now", 60)

    def set_bus_schedule_sensors(self, kwargs):
        schedules = self.get_bus_schedules(6001350, "Huskvarna via Centrum-Elmia")
        self.update_sensors(schedules, "north")

        schedules = self.get_bus_schedules(6001353, "Jönköping Rådhusparken")
        self.update_sensors(schedules, "north")

    def update_sensors(self, schedules, suffix):

        index = 1
        for schedule in schedules:
            prefix = f"sensor.jlt_bus_{schedule['line_number']}_{suffix}"
            sensor_name = f"{prefix}_departure_{index}"

            # Departure time helper
            # Stupid automations trigger on both state change and attribute state change,
            # so need a non "time_to_departure" attribute one for E-Paper displays.
            departure_name = f"{sensor_name}_departure"
            self.set_state(departure_name, state=schedule['departure_time'])

            # Time to departure helper
            time_to_departure_time_name = f"{sensor_name}_time_to_departure"
            self.set_state(time_to_departure_time_name, state=schedule['time_to_departure'])

            # Too late helper
            too_late_name = f"{sensor_name}_too_late"
            self.set_state(too_late_name, state=schedule['too_late'])

            # Make main sensor
            attributes = {}
            attributes.update({"line_number": schedule['line_number']})
            attributes.update({"direction": schedule['direction']})
            attributes.update({"time_to_departure": schedule['time_to_departure']})
            attributes.update({"too_late": schedule["too_late"]})
            self.log(
                f"{sensor_name}: {schedule['departure_time']}{schedule['time_to_departure']}, (line_number: {schedule['line_number']}, direction: {schedule['direction']})")
            self.set_state(sensor_name, state=schedule['departure_time'], attributes=attributes)

            index = index + 1
            if index == 10:
                return

    def get_bus_schedules(self, stop_id, destination):
        schedule_list = []

        url = f"https://www.jlt.se/api/StopAreaApi/GetClosestDepartures?fromId={stop_id}&take=20"
        response = requests.get(url)
        soup = BeautifulSoup(response.content, "html.parser")

        travel_suggestions = soup.select("div.travel-suggestion")

        for suggestion in travel_suggestions:
            direction_element = suggestion.select_one("div.direction")
            direction = direction_element.get_text(strip=True)[5:]
            line_number_element = suggestion.select_one("div.line-info span")
            line_number = line_number_element.get_text(strip=True)
            if direction_element and destination in direction_element.get_text(strip=True):
                time_element = suggestion.select_one("p")
                if time_element:
                    time = time_element.get_text().strip()[0:5]
                    time_to_departure = time_element.get_text().strip()[7:-1]
                    now = datetime.now()
                    target_time = datetime.strptime(time, "%H:%M").replace(
                        year=now.year, month=now.month, day=now.day
                    )
                    treshold = timedelta(minutes=8)
                    too_late = now > target_time - treshold
                    if time_to_departure != "Har avgått":
                        schedule_list.append({
                            "line_number": line_number,
                            "departure_time": time,
                            "direction": direction,
                            "time_to_departure": time_to_departure,
                            "too_late": too_late
                        })
        return schedule_list
