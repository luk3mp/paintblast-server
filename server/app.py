import os
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
import math
import time
from threading import Lock
import logging
import uuid
from collections import deque

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('paintblast')

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
    },
    'queue': deque()  # Queue for waiting players, using deque for efficient operations
}

# Server status information
server_status = {
    'currentPlayers': 0,
    'maxPlayers': 100,
    'queueLength': 0,
    'redTeamPlayers': 0,
    'blueTeamPlayers': 0
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
MAX_PLAYERS = 100  # Maximum players
MAX_PLAYERS_PER_TEAM = 50  # To ensure team balance

# Thread safety
game_state_lock = Lock()

def broadcast_game_state():
    """Broadcast the current game state to all clients."""
    socketio.emit('gameState', game_state)

def update_server_status():
    """Update server status information."""
    with game_state_lock:
        server_status['currentPlayers'] = len(game_state['players'])
        server_status['queueLength'] = len(game_state['queue'])
        
        # Count players by team
        red_count = sum(1 for player in game_state['players'].values() if player['team'] == 'red')
        blue_count = sum(1 for player in game_state['players'].values() if player['team'] == 'blue')
        
        server_status['redTeamPlayers'] = red_count
        server_status['blueTeamPlayers'] = blue_count

def broadcast_server_status():
    """Broadcast server status to all clients."""
    update_server_status()
    socketio.emit('serverStatus', server_status)

def estimate_wait_time(position):
    """Estimate wait time based on queue position."""
    # Simple estimation: assume 30 seconds per player ahead in queue
    # This could be improved based on actual game statistics
    return position * 30  # seconds

def process_queue():
    """Process the queue and allow players to join if space is available."""
    with game_state_lock:
        # Check if there's room for more players
        if len(game_state['players']) < MAX_PLAYERS and game_state['queue']:
            # Get the next player in the queue
            next_player = game_state['queue'].popleft()
            
            # Notify the player they can join
            socketio.emit('queueReady', room=next_player['sid'])
            logger.info(f"Player {next_player['name']} can now join the game!")
            
            # Update queue positions for remaining players
            update_queue_positions()
            
            # Update and broadcast server status
            broadcast_server_status()

def update_queue_positions():
    """Update queue positions for all queued players."""
    for i, player in enumerate(game_state['queue']):
        socketio.emit('queueUpdate', {
            'position': i + 1,
            'estimatedWaitTime': estimate_wait_time(i)
        }, room=player['sid'])

def assign_team(preferred_team=None):
    """Assign a team to a new player, considering team balance."""
    with game_state_lock:
        red_count = sum(1 for player in game_state['players'].values() if player['team'] == 'red')
        blue_count = sum(1 for player in game_state['players'].values() if player['team'] == 'blue')
        
        # If teams are unbalanced by more than 1 player, assign to smaller team
        if abs(red_count - blue_count) > 1:
            return 'red' if red_count < blue_count else 'blue'
        
        # If preferred team is provided and wouldn't cause imbalance, use it
        if preferred_team in ['red', 'blue']:
            if preferred_team == 'red' and red_count <= blue_count:
                return 'red'
            elif preferred_team == 'blue' and blue_count <= red_count:
                return 'blue'
        
        # Otherwise, assign randomly but favor the smaller team
        return 'red' if red_count <= blue_count else 'blue'

@socketio.on('connect')
def handle_connect():
    """Handle new client connection."""
    logger.info(f"Client connected: {request.sid}")
    # Send initial server status to the new client
    update_server_status()
    socketio.emit('serverStatus', server_status, room=request.sid)

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.info(f"Client disconnected: {request.sid}")
    
    with game_state_lock:
        # Check if player was in the game
        if request.sid in game_state['players']:
            # Remove player from game
            player = game_state['players'].pop(request.sid)
            logger.info(f"Player {player['name']} left the game")
            
            # Drop flag if player was carrying it
            if game_state['flags']['red']['carrier'] == request.sid:
                game_state['flags']['red']['captured'] = False
                game_state['flags']['red']['carrier'] = None
                socketio.emit('flagReturned', {'team': 'red'})
            
            if game_state['flags']['blue']['carrier'] == request.sid:
                game_state['flags']['blue']['captured'] = False
                game_state['flags']['blue']['carrier'] = None
                socketio.emit('flagReturned', {'team': 'blue'})
            
            # Process queue to let a waiting player join
            process_queue()
        
        # Check if player was in the queue
        else:
            # Find and remove player from queue if present
            for i, player in enumerate(game_state['queue']):
                if player['sid'] == request.sid:
                    game_state['queue'].remove(player)
                    logger.info(f"Player {player['name']} left the queue")
                    # Update queue positions
                    update_queue_positions()
                    break
    
    # Update and broadcast server status
    broadcast_server_status()
    # Broadcast player list update
    socketio.emit('players', game_state['players'])

@socketio.on('join')
def handle_join(data):
    """Handle player join request."""
    player_name = data.get('name', f"Player_{uuid.uuid4().hex[:6]}")
    preferred_team = data.get('team', '').lower()
    
    with game_state_lock:
        # Check if game has space for the player
        if len(game_state['players']) < MAX_PLAYERS:
            # Assign team
            team = assign_team(preferred_team)
            
            # Create player object
            player = {
                'name': player_name,
                'team': team,
                'health': 100,
                'position': [0, 2, -110] if team == 'red' else [0, 2, 110],
                'rotation': [0, 0, 0],
                'score': 0,
                'kills': 0,
                'deaths': 0,
                'joinTime': time.time()
            }
            
            # Add player to game
            game_state['players'][request.sid] = player
            logger.info(f"Player {player_name} joined as {team}")
            
            # Send join success
            socketio.emit('joinSuccess', {
                'id': request.sid,
                'team': team
            }, room=request.sid)
            
            # Broadcast player list update
            socketio.emit('players', game_state['players'])
            
            # Update and broadcast server status
            broadcast_server_status()
        else:
            # Server is full, add player to queue
            queue_player = {
                'sid': request.sid,
                'name': player_name,
                'preferred_team': preferred_team,
                'joinTime': time.time()
            }
            
            # Add to queue
            game_state['queue'].append(queue_player)
            
            # Calculate queue position (1-based)
            position = len(game_state['queue'])
            
            logger.info(f"Server full, {player_name} added to queue at position {position}")
            
            # Send queue position to player
            socketio.emit('queueUpdate', {
                'position': position,
                'estimatedWaitTime': estimate_wait_time(position - 1)
            }, room=request.sid)
            
            # Update and broadcast server status
            broadcast_server_status()

@socketio.on('message')
def handle_message(data):
    """Handle chat messages."""
    if request.sid in game_state['players']:
        player = game_state['players'][request.sid]
        
        # Create message object
        message = {
            'sender': player['name'],
            'team': player['team'],
            'text': data.get('text', ''),
            'timestamp': data.get('timestamp', time.time())
        }
        
        # Broadcast message to all players
        socketio.emit('message', message)

@socketio.on('updatePosition')
def handle_update_position(data):
    """Handle player position updates."""
    with game_state_lock:
        if request.sid in game_state['players']:
            # Update player position
            player = game_state['players'][request.sid]
            position = data.get('position')
            rotation = data.get('rotation')
            
            if position:
                player['position'] = position
            
            if rotation:
                player['rotation'] = rotation
            
            # Check for flag proximity and interactions
            check_flag_interactions(request.sid, player)

def check_flag_interactions(player_sid, player):
    """Check and handle player interactions with flags."""
    # Check if player can capture enemy flag
    if player['team'] == 'red':
        flag_team = 'blue'
        enemy_base = [0, 0, 110]
        home_base = [0, 0, -110]
    else:
        flag_team = 'red'
        enemy_base = [0, 0, -110]
        home_base = [0, 0, 110]
    
    # Get player position as vector
    player_pos = player['position']
    
    # Check if player is near enemy flag
    enemy_flag = game_state['flags'][flag_team]
    
    if not enemy_flag['captured']:
        # Calculate distance to flag
        flag_pos = enemy_flag['position']
        distance = math.sqrt(
            (player_pos[0] - flag_pos[0])**2 + 
            (player_pos[1] - flag_pos[1])**2 + 
            (player_pos[2] - flag_pos[2])**2
        )
        
        # If player is close enough, capture flag
        if distance < FLAG_CAPTURE_RADIUS:
            enemy_flag['captured'] = True
            enemy_flag['carrier'] = player_sid
            
            logger.info(f"Player {player['name']} captured the {flag_team} flag!")
            
            # Broadcast flag capture
            socketio.emit('flagCaptured', {
                'team': flag_team,
                'carrier': player['name']
            })
    
    # Check if player carrying flag is at home base to score
    if game_state['flags'][flag_team]['carrier'] == player_sid:
        # Calculate distance to home base
        distance_to_home = math.sqrt(
            (player_pos[0] - home_base[0])**2 +
            (player_pos[1] - home_base[1])**2 +
            (player_pos[2] - home_base[2])**2
        )
        
        # If player is close enough to home base, score
        if distance_to_home < FLAG_CAPTURE_RADIUS:
            # Reset flag
            game_state['flags'][flag_team]['captured'] = False
            game_state['flags'][flag_team]['carrier'] = None
            
            # Update score
            game_state['scores'][player['team']] += FLAG_SCORE_POINTS
            
            logger.info(f"Player {player['name']} scored with the {flag_team} flag!")
            
            # Broadcast flag score
            socketio.emit('flagScored', {
                'team': flag_team,
                'scorer': player['name'],
                'redScore': game_state['scores']['red'],
                'blueScore': game_state['scores']['blue']
            })
            
            # Check win condition
            if game_state['scores'][player['team']] >= WIN_SCORE:
                socketio.emit('gameOver', {
                    'winner': player['team'],
                    'redScore': game_state['scores']['red'],
                    'blueScore': game_state['scores']['blue']
                })
                
                # Reset scores for next game
                game_state['scores']['red'] = 0
                game_state['scores']['blue'] = 0

@socketio.on('captureFlag')
def handle_capture_flag(data):
    """Handle client-initiated flag capture (validation still done server-side)."""
    flag_team = data.get('team')
    
    if flag_team not in ['red', 'blue']:
        return
    
    with game_state_lock:
        if request.sid in game_state['players']:
            player = game_state['players'][request.sid]
            
            # Only allow capturing enemy flag
            if (player['team'] == 'red' and flag_team == 'blue') or \
               (player['team'] == 'blue' and flag_team == 'red'):
                
                # Extra validation through check_flag_interactions
                check_flag_interactions(request.sid, player)

@socketio.on('scoreFlag')
def handle_score_flag(data):
    """Handle client-initiated flag scoring (validation still done server-side)."""
    flag_team = data.get('team')
    
    if flag_team not in ['red', 'blue']:
        return
    
    with game_state_lock:
        if request.sid in game_state['players']:
            player = game_state['players'][request.sid]
            
            # Check if player is carrying the flag
            if game_state['flags'][flag_team]['carrier'] == request.sid:
                # Extra validation through check_flag_interactions
                check_flag_interactions(request.sid, player)

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
                        handle_player_elimination(data['target'], shooter_id)
                    
                    broadcast_game_state()

def handle_player_elimination(target_id, shooter_id):
    """Handle player elimination (being hit with 0 health)."""
    target = game_state['players'][target_id]
    shooter = game_state['players'][shooter_id]
    
    # Update stats
    target['deaths'] += 1
    shooter['kills'] += 1
    shooter['score'] += 1
    
    # Reset target health
    target['health'] = 100
    
    # Drop flag if player was carrying it
    if game_state['flags']['red']['carrier'] == target_id:
        game_state['flags']['red']['captured'] = False
        game_state['flags']['red']['carrier'] = None
        socketio.emit('flagReturned', {'team': 'red'})
    
    if game_state['flags']['blue']['carrier'] == target_id:
        game_state['flags']['blue']['captured'] = False
        game_state['flags']['blue']['carrier'] = None
        socketio.emit('flagReturned', {'team': 'blue'})
    
    # Emit kill event
    socketio.emit('playerKilled', {
        'player': target['name'],
        'killer': shooter['name']
    })
    
    # Respawn player at their team's base
    if target['team'] == 'red':
        target['position'] = [0, 2, -110]  # Red base
    else:
        target['position'] = [0, 2, 110]  # Blue base

@app.route('/status')
def server_status_endpoint():
    """API endpoint to get server status."""
    update_server_status()
    return jsonify(server_status)

@socketio.on('requestServerStatus')
def handle_server_status_request():
    """Handle client request for server status."""
    update_server_status()
    socketio.emit('serverStatus', server_status, room=request.sid)

# Set up regular server status broadcasts
def background_task():
    """Background task to periodically broadcast server status."""
    while True:
        broadcast_server_status()
        socketio.sleep(5)  # Update every 5 seconds

if __name__ == '__main__':
    logger.info("Starting PaintBlast server...")
    socketio.start_background_task(background_task)
    socketio.run(app, host='0.0.0.0', port=8000, debug=True, allow_unsafe_werkzeug=True) 