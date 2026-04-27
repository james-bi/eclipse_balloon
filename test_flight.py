from flight_loop import FlightComputer

fc = FlightComputer()

# Force 5 mock readings for ascending
for alt in [1000, 2000, 3000, 4500, 4600]:
    phase = fc.update_phase(alt)
    print(f"Alt: {alt}, Phase: {phase}")

# Now force descent
for alt in [4500, 4400, 4300, 4200]:
    phase = fc.update_phase(alt)
    print(f"Alt: {alt}, Phase: {phase}")

# Now force landing
for alt in [90, 80]:
    phase = fc.update_phase(alt)
    print(f"Alt: {alt}, Phase: {phase}")

# Test calculate_descent_rate and landing time
fc.safety_manager.altitude_history.append(4400)
fc.safety_manager.altitude_history.append(4300)
fc.safety_manager.altitude_history.append(4200)

print("Descent rate:", fc.safety_manager.calculate_descent_rate())
print("Time to land:", fc.safety_manager.calculate_landing_time(4200))
