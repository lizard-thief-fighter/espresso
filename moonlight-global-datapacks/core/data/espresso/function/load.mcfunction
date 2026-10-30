# Runs on every load; applies default gamerules only the first time a world loads.
execute unless data storage espresso:state {gamerules_applied:1b} run function espresso:set_gamerules
