import os
from flask import Flask, request
from flask_socketio import SocketIO, emit
import math
import time
from threading import Lock

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Game state
game_state = {
    'players': {},
    'status': 'waiting',
    'scores': {'red': 0, 'blue': 0},
    'flags': {
        'red': {'captured': False, 'carrier': None, 'position': [0, 0, -110]},
        'blue': {'captured': False, 'carrier': None, 'position': [0, 0, 110]}
    }
}

# Game constants
PLAYER_RADIUS = 1.0
PAINTBALL_RADIUS = 0.2
HIT_DAMAGE = 25
RESPAWN_TIME = 5
FLAG_CAPTURE_RADIUS = 4
FLAG_SCORE_POINTS = 1
MIN_PLAYERS_PER_TEAM = 1
WIN_SCORE = 3

# Thread safety
game_state_lock = Lock()

def broadcast_game_state():
    """Broadcast the current game state to all clients."""
    socketio.emit('gameState', game_state)

@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    print(f"Client connected: {request.sid}")
    broadcast_game_state()

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    with game_state_lock:
        if request.sid in game_state['players']:
            player = game_state['players'][request.sid]
            team = player['team']
            player_name = player['name']
            
            # Return flag if player was carrying it
            if game_state['flags']['red']['carrier'] == request.sid:
                game_state['flags']['red']['captured'] = False
                game_state['flags']['red']['carrier'] = None
                # Notify all clients
                socketio.emit('flagReturned', {'team': 'red'}, broadcast=True)
                print(f"Red flag returned because {player_name} disconnected")
            elif game_state['flags']['blue']['carrier'] == request.sid:
                game_state['flags']['blue']['captured'] = False
                game_state['flags']['blue']['carrier'] = None
                # Notify all clients
                socketio.emit('flagReturned', {'team': 'blue'}, broadcast=True)
                print(f"Blue flag returned because {player_name} disconnected")
            
            del game_state['players'][request.sid]
            print(f"Player {player_name} ({team} team) disconnected")
            
            # Check if game should end
            check_game_state()
            broadcast_game_state()

@socketio.on('join')
def handle_join(data):
    """Handle player join request."""
    with game_state_lock:
        name = data.get('name', f"Player_{request.sid[:4]}")
        team = data.get('team', 'red')  # Default to red team if not specified
        
        # Get position from client or use default spawn based on team
        client_position = data.get('position')
        if client_position:
            position = client_position
        else:
            # Assign spawn position based on team
            spawn_z = -110 if team == 'red' else 110
            position = [0, 2, spawn_z]
        
        game_state['players'][request.sid] = {
            'name': name,
            'team': team,
            'position': position,
            'rotation': [0, 0, 0],
            'health': 100,
            'score': 0,
            'respawning': False,
            'respawn_time': None
        }
        
        print(f"Player {name} joined {team} team at position {position}")
        check_game_state()
        broadcast_game_state()

def check_game_state():
    """Check if game should start/end based on player count."""
    with game_state_lock:
        red_count = sum(1 for p in game_state['players'].values() if p['team'] == 'red')
        blue_count = sum(1 for p in game_state['players'].values() if p['team'] == 'blue')
        
        if game_state['status'] == 'waiting':
            if red_count >= MIN_PLAYERS_PER_TEAM and blue_count >= MIN_PLAYERS_PER_TEAM:
                start_game()
        elif game_state['status'] == 'playing':
            if red_count < MIN_PLAYERS_PER_TEAM or blue_count < MIN_PLAYERS_PER_TEAM:
                end_game('insufficient_players')

def start_game():
    """Start the game."""
    with game_state_lock:
        game_state['status'] = 'playing'
        game_state['scores'] = {'red': 0, 'blue': 0}
        game_state['flags'] = {
            'red': {'captured': False, 'carrier': None, 'position': [0, 0, -110]},
            'blue': {'captured': False, 'carrier': None, 'position': [0, 0, 110]}
        }
        socketio.emit('gameStart', {'message': 'Game started!'})
        broadcast_game_state()

def end_game(reason='game_over'):
    """End the game."""
    with game_state_lock:
        game_state['status'] = 'ended'
        socketio.emit('gameOver', {
            'reason': reason,
            'scores': game_state['scores']
        })
        # Reset game state after a delay
        socketio.sleep(5)
        game_state['status'] = 'waiting'
        broadcast_game_state()

@socketio.on('updatePosition')
def handle_position_update(data):
    """Handle player position updates."""
    with game_state_lock:
        if request.sid in game_state['players']:
            player = game_state['players'][request.sid]
            player['position'] = data['position']
            player['rotation'] = data['rotation']
            
            # Update flag position if player is carrying it
            if game_state['flags']['red']['carrier'] == request.sid:
                game_state['flags']['red']['position'] = data['position']
            elif game_state['flags']['blue']['carrier'] == request.sid:
                game_state['flags']['blue']['position'] = data['position']
            
            # Check for flag proximity
            check_flag_interactions(request.sid, data['position'])
            
            # Check for canister crate proximity
            check_canister_proximity(request.sid, data['position'])
            
            # Broadcast position updates less frequently than other game state updates
            emit('playerPosition', {
                'id': request.sid,
                'position': data['position'],
                'rotation': data['rotation']
            }, broadcast=True)

def check_flag_interactions(player_id, position):
    """Check if player is near flags and handle both capturing and scoring."""
    with game_state_lock:
        if player_id not in game_state['players']:
            return
        
        player = game_state['players'][player_id]
        player_team = player['team']
        enemy_team = 'blue' if player_team == 'red' else 'red'
        
        # Check if near enemy flag for capturing
        enemy_flag = game_state['flags'][enemy_team]
        if not enemy_flag['captured']:  # Only check if flag is not already captured
            enemy_flag_pos = enemy_flag['position']
            distance_to_enemy_flag = math.sqrt(
                (position[0] - enemy_flag_pos[0])**2 +
                (position[2] - enemy_flag_pos[2])**2
            )
            
            # If near enemy flag, player can capture it
            if distance_to_enemy_flag < FLAG_CAPTURE_RADIUS:
                # Notify the player they can capture the flag
                socketio.emit('nearEnemyFlag', {
                    'team': enemy_team,
                    'position': enemy_flag_pos
                }, room=player_id)
        
        # Check if carrying flag and near home base for scoring
        is_carrying_enemy_flag = (
            (player_team == 'red' and game_state['flags']['blue']['carrier'] == player_id) or
            (player_team == 'blue' and game_state['flags']['red']['carrier'] == player_id)
        )
        
        if is_carrying_enemy_flag:
            # Check if near home base
            home_base_pos = [0, 0, -110] if player_team == 'red' else [0, 0, 110]
            distance_to_home = math.sqrt(
                (position[0] - home_base_pos[0])**2 +
                (position[2] - home_base_pos[2])**2
            )
            
            # If near home base with enemy flag, player can score
            if distance_to_home < FLAG_CAPTURE_RADIUS:
                # Notify the player they can score
                socketio.emit('nearHomeBase', {
                    'canScore': True
                }, room=player_id)

def check_canister_proximity(player_id, position):
    """Check if player is near canister replenishment station."""
    with game_state_lock:
        if player_id not in game_state['players']:
            return
        
        player = game_state['players'][player_id]
        team = player['team']
        
        # Get coordinates of team's canister crate
        red_crate_pos = [0, 0, -130]  # Red team crate at back wall
        blue_crate_pos = [0, 0, 130]  # Blue team crate at back wall
        
        # Calculate distance to player's team crate
        team_crate_pos = red_crate_pos if team == 'red' else blue_crate_pos
        distance = math.sqrt(
            (position[0] - team_crate_pos[0])**2 +
            (position[2] - team_crate_pos[2])**2
        )
        
        # If player is within range of their team's crate, notify them
        if distance < 5:
            socketio.emit('nearCanisterCrate', {'player': player_id}, room=player_id)

@socketio.on('shoot')
def handle_shoot(data):
    """Handle player shooting."""
    with game_state_lock:
        if request.sid in game_state['players']:
            shooter = game_state['players'][request.sid]
            
            # Broadcast paintball to all players
            socketio.emit('paintball', {
                'id': f"pb_{time.time()}_{request.sid}",
                'origin': data['origin'],
                'direction': data['direction'],
                'color': '#ff4500' if shooter['team'] == 'red' else '#0066ff'
            }, broadcast=True)

@socketio.on('hit')
def handle_hit(data):
    """Handle paintball hits."""
    with game_state_lock:
        if data['target'] in game_state['players']:
            target = game_state['players'][data['target']]
            shooter_id = data.get('shooter')
            
            if shooter_id and shooter_id in game_state['players']:
                shooter = game_state['players'][shooter_id]
                if shooter['team'] != target['team']:  # Only allow hits on enemy team
                    # Apply damage
                    target['health'] -= HIT_DAMAGE
                    
                    # Check if player is eliminated
                    if target['health'] <= 0:
                        handle_player_elimination(data['target'])
                    
                    broadcast_game_state()

def handle_player_elimination(player_id):
    """Handle player elimination."""
    with game_state_lock:
        player = game_state['players'][player_id]
        player['respawning'] = True
        player['respawn_time'] = time.time() + RESPAWN_TIME
        player_name = player['name']
        
        # Drop flag if carrying
        if game_state['flags']['red']['carrier'] == player_id:
            game_state['flags']['red']['captured'] = False
            game_state['flags']['red']['carrier'] = None
            # Notify all clients
            socketio.emit('flagReturned', {'team': 'red'}, broadcast=True)
            print(f"Red flag returned because {player_name} was eliminated")
        elif game_state['flags']['blue']['carrier'] == player_id:
            game_state['flags']['blue']['captured'] = False
            game_state['flags']['blue']['carrier'] = None
            # Notify all clients
            socketio.emit('flagReturned', {'team': 'blue'}, broadcast=True)
            print(f"Blue flag returned because {player_name} was eliminated")
        
        # Schedule respawn
        def respawn_player():
            with game_state_lock:
                if player_id in game_state['players']:
                    player = game_state['players'][player_id]
                    player['health'] = 100
                    player['respawning'] = False
                    player['respawn_time'] = None
                    # Respawn at team base
                    spawn_z = -110 if player['team'] == 'red' else 110
                    player['position'] = [0, 2, spawn_z]
                    broadcast_game_state()
        
        socketio.sleep(RESPAWN_TIME)
        respawn_player()

@socketio.on('captureFlag')
def handle_flag_capture(data):
    """Handle flag capture attempts."""
    with game_state_lock:
        if request.sid not in game_state['players']:
            return
        
        player = game_state['players'][request.sid]
        team_to_capture = data.get('team')
        
        # Validate team to capture
        if team_to_capture not in ['red', 'blue']:
            print(f"Invalid team to capture: {team_to_capture}")
            return
            
        # Can only capture enemy flag
        if (player['team'] == 'red' and team_to_capture == 'red') or \
           (player['team'] == 'blue' and team_to_capture == 'blue'):
            print(f"Player {player['name']} attempted to capture their own team's flag")
            return
        
        # Check if flag is already captured
        if game_state['flags'][team_to_capture]['captured']:
            print(f"{team_to_capture} flag is already captured")
            return
        
        # Check proximity to flag
        player_pos = player['position']
        flag_pos = game_state['flags'][team_to_capture]['position']
        distance = math.sqrt(
            (player_pos[0] - flag_pos[0])**2 +
            (player_pos[2] - flag_pos[2])**2
        )
        
        if distance > FLAG_CAPTURE_RADIUS:
            print(f"Player {player['name']} is too far from {team_to_capture} flag to capture it. Distance: {distance}")
            return
            
        # Capture the flag
        game_state['flags'][team_to_capture]['captured'] = True
        game_state['flags'][team_to_capture]['carrier'] = request.sid
        
        print(f"Player {player['name']} captured the {team_to_capture} flag!")
        
        # Notify all clients about the flag capture
        socketio.emit('flagCaptured', {
            'team': team_to_capture,
            'carrier': player['name']
        }, broadcast=True)
        
        broadcast_game_state()

@socketio.on('scoreFlag')
def handle_flag_score(data):
    """Handle flag scoring attempts."""
    with game_state_lock:
        if request.sid not in game_state['players']:
            return
        
        player = game_state['players'][request.sid]
        team_to_score = data.get('team')
        
        # Validate team to score
        if team_to_score not in ['red', 'blue']:
            print(f"Invalid team to score: {team_to_score}")
            return
            
        # Can only score enemy flag
        if (player['team'] == 'red' and team_to_score == 'red') or \
           (player['team'] == 'blue' and team_to_score == 'blue'):
            print(f"Player {player['name']} attempted to score their own team's flag")
            return
        
        # Check if player is actually carrying the flag
        if game_state['flags'][team_to_score]['carrier'] != request.sid:
            print(f"Player {player['name']} is not carrying the {team_to_score} flag")
            return
        
        # Check proximity to home base
        player_pos = player['position']
        home_base_pos = [0, 0, -110] if player['team'] == 'red' else [0, 0, 110]
        distance = math.sqrt(
            (player_pos[0] - home_base_pos[0])**2 +
            (player_pos[2] - home_base_pos[2])**2
        )
        
        if distance > FLAG_CAPTURE_RADIUS:
            print(f"Player {player['name']} is too far from their base to score. Distance: {distance}")
            return
            
        # Add score
        game_state['scores'][player['team']] += FLAG_SCORE_POINTS
        player['score'] += FLAG_SCORE_POINTS
        
        # Reset flag
        game_state['flags'][team_to_score]['captured'] = False
        game_state['flags'][team_to_score]['carrier'] = None
        game_state['flags'][team_to_score]['position'] = [0, 0, 110 if team_to_score == 'blue' else -110]
        
        print(f"Player {player['name']} scored with the {team_to_score} flag! {player['team']} team now has {game_state['scores'][player['team']]} points")
        
        # Notify all clients about the flag score
        socketio.emit('flagScored', {
            'team': team_to_score,
            'carrier': player['name'],
            'redScore': game_state['scores']['red'],
            'blueScore': game_state['scores']['blue']
        }, broadcast=True)
        
        broadcast_game_state()
        
        # Check for game end condition
        if game_state['scores'][player['team']] >= WIN_SCORE:
            end_game('victory')

@socketio.on('message')
def handle_message(data):
    """Handle chat messages."""
    if request.sid in game_state['players']:
        player = game_state['players'][request.sid]
        message = {
            'sender': player['name'],
            'team': player['team'],
            'text': data['text'],
            'timestamp': data['timestamp']
        }
        socketio.emit('message', message, broadcast=True)

# Add a dedicated handler for canister replenishment
@socketio.on('replenish')
def handle_canister_replenish():
    """Handle canister replenishment at base stations."""
    with game_state_lock:
        if request.sid not in game_state['players']:
            return
        
        player = game_state['players'][request.sid]
        position = player['position']
        team = player['team']
        
        # Check if player is at their team's canister station
        red_station_pos = [0, 0, -130]  # Red team station position
        blue_station_pos = [0, 0, 130]  # Blue team station position
        
        home_station_pos = red_station_pos if team == 'red' else blue_station_pos
        
        # Calculate distance to the player's team station
        distance = math.sqrt(
            (position[0] - home_station_pos[0])**2 +
            (position[2] - home_station_pos[2])**2
        )
        
        # If player is within 5 units of their station, allow replenishment
        if distance < 5:
            print(f"Player {player['name']} replenished canisters at {team} base")
            # No need to modify server-side state as ammo is client-side
            # Just acknowledge the replenishment
            socketio.emit('replenishComplete', room=request.sid)
        else:
            print(f"Player {player['name']} tried to replenish but is not near {team} station. Distance: {distance}")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=8000, debug=True, allow_unsafe_werkzeug=True) 