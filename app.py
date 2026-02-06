import eventlet
eventlet.monkey_patch()

import os
from flask import Flask, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
import math
import time
from threading import Lock, RLock, Timer
import logging
import uuid
from collections import deque
import json
import gzip
import base64
import random
import zlib

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('paintblast')

# Update Gunicorn configuration
class GunicornConfig:
    """Gunicorn configuration to prevent worker timeouts."""
    # Increase timeout to 300 seconds (5 minutes)
    timeout = 300
    # Ensure we're using eventlet
    worker_class = 'eventlet'
    # Single worker for multiplayer consistency
    workers = 1
    # Increase threads per worker for better concurrency
    threads = 4
    # Log level
    loglevel = 'info'
    # Increase keep-alive timeout
    keepalive = 120
    # Prevent worker crashes from terminating the application
    max_requests = 1000
    max_requests_jitter = 50
    graceful_timeout = 10

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key')

# CORS Configuration
cors_origins = [
    "http://localhost:3000",  # Local frontend development
    "https://paintblast.vercel.app",  # Vercel deployment URL
    "https://paintblast.luke-mp.co",  # Custom subdomain (updated domain)
    "https://paintblast.lukemp.co",  # Legacy subdomain (keep for backwards compatibility)
    "https://paintblast-server.onrender.com",  # Render domain for the server itself
    "https://paintblast-git-main-luk3mps-projects.vercel.app",  # Vercel preview deployment
    "https://paintblast-luk3mps-projects.vercel.app",  # Vercel project deployment
]

# Set up SocketIO with proper configuration for Gunicorn
# ping_timeout and ping_interval help with keeping connections alive
socketio = SocketIO(
    app, 
    cors_allowed_origins=cors_origins, 
    async_mode='eventlet',
    ping_timeout=60,
    ping_interval=25,
    manage_session=False,  # Let Flask handle sessions for better stability
    logger=True,  # Enable SocketIO internal logging
    engineio_logger=True,  # Enable Engine.IO internal logging
    max_http_buffer_size=1e8,  # Increase buffer size to 100MB
    always_connect=True  # Always accept connections
)

# Apply CORS to regular HTTP routes
CORS(app, origins=cors_origins, supports_credentials=True)

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

# Performance optimization settings
POSITION_UPDATE_THRESHOLD = 0.5  # Minimum position change to trigger update
ROTATION_UPDATE_THRESHOLD = 0.1  # Minimum rotation change to trigger update
BROADCAST_RATE_LIMIT = 100  # Milliseconds between broadcast updates
PLAYER_UPDATE_BATCH_SIZE = 20  # Send player updates in batches
ENABLE_COMPRESSION = True  # Enable message compression for large payloads
COMPRESSION_THRESHOLD = 1024  # Only compress messages larger than 1KB

# Game constants
PLAYER_RADIUS = 1.0
PAINTBALL_RADIUS = 0.2
HIT_DAMAGE = 25
RESPAWN_TIME = 5  # Seconds for respawn delay
FLAG_CAPTURE_RADIUS = 4
FLAG_SCORE_POINTS = 1
MIN_PLAYERS_PER_TEAM = 1
WIN_SCORE = 3
MAX_PLAYERS = 100  # Maximum players
MAX_PLAYERS_PER_TEAM = 50  # To ensure team balance
BASE_SPAWN_RADIUS = 5 # Radius around base center for randomized spawn

# Thread safety - use RLock (reentrant) to allow nested lock acquisition
# (e.g. assign_team and update_server_status called from within handle_join's lock)
game_state_lock = RLock()

# Performance tracking
last_broadcast_time = time.time()
server_stats = {
    'messages_received': 0,
    'messages_sent': 0,
    'bytes_received': 0,
    'bytes_sent': 0,
    'position_updates': 0,
    'start_time': time.time()
}

def compress_data(data):
    """Compress data if it exceeds threshold."""
    if not ENABLE_COMPRESSION:
        return data, False
    
    # Serialize data to JSON
    json_data = json.dumps(data)
    
    # Check if compression is worth it
    if len(json_data) < COMPRESSION_THRESHOLD:
        return data, False
    
    # Compress
    compressed = gzip.compress(json_data.encode())
    encoded = base64.b64encode(compressed).decode()
    
    return encoded, True

def broadcast_game_state():
    """Broadcast the current game state to all clients."""
    global last_broadcast_time
    
    # Rate limit broadcasts
    now = time.time()
    if now - last_broadcast_time < BROADCAST_RATE_LIMIT / 1000:
        return
    
    last_broadcast_time = now
    
    # Compress if large
    compressed_data, is_compressed = compress_data(game_state)
    
    if is_compressed:
        socketio.emit('gameStateCompressed', compressed_data)
        # Track compressed data size
        server_stats['bytes_sent'] += len(compressed_data)
    else:
        socketio.emit('gameState', compressed_data)
        # Approximate data size
        server_stats['bytes_sent'] += len(json.dumps(compressed_data))
    
    server_stats['messages_sent'] += 1

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
    server_stats['messages_sent'] += 1

def get_players_for_client():
    """Get player data with capitalized team names for client compatibility."""
    players_for_client = {}
    for pid, pdata in game_state['players'].items():
        players_for_client[pid] = {
            **pdata,
            'team': pdata['team'].capitalize() if pdata.get('team') else 'Red'
        }
    return players_for_client

def broadcast_players_batch():
    """Broadcast player state (full data)."""
    with game_state_lock:
        if not game_state['players']:
            return

        # Send player data with capitalized team names for client
        players_for_client = get_players_for_client()

    socketio.emit('players', players_for_client)
    server_stats['messages_sent'] += 1

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
    """Assign a team to a new player, considering team balance.
    NOTE: Must be called while game_state_lock is already held (do NOT acquire lock here).
    Returns lowercase team name ('red' or 'blue').
    """
    red_count = sum(1 for player in game_state['players'].values() if player['team'] == 'red')
    blue_count = sum(1 for player in game_state['players'].values() if player['team'] == 'blue')
    
    # Normalize preferred_team to lowercase for comparison
    normalized_pref = preferred_team.lower() if preferred_team else None
    
    # If teams are unbalanced by more than 1 player, assign to smaller team
    if abs(red_count - blue_count) > 1:
        return 'red' if red_count < blue_count else 'blue'
    
    # If preferred team is provided and wouldn't cause imbalance, use it
    if normalized_pref in ['red', 'blue']:
        if normalized_pref == 'red' and red_count <= blue_count:
            return 'red'
        elif normalized_pref == 'blue' and blue_count <= red_count:
            return 'blue'
    
    # Otherwise, assign randomly but favor the smaller team
    return 'red' if red_count <= blue_count else 'blue'

def position_changed_significantly(new_pos, old_pos):
    """Check if position changed enough to warrant an update."""
    if not old_pos:
        return True
    
    dx = new_pos[0] - old_pos[0]
    dy = new_pos[1] - old_pos[1]
    dz = new_pos[2] - old_pos[2]
    
    # Calculate distance squared (faster than using math.sqrt)
    distance_sq = dx * dx + dy * dy + dz * dz
    
    return distance_sq > POSITION_UPDATE_THRESHOLD * POSITION_UPDATE_THRESHOLD

def rotation_changed_significantly(new_rot, old_rot):
    """Check if rotation changed enough to warrant an update."""
    if not old_rot:
        return True
    
    # Check each axis
    for i in range(min(len(new_rot), len(old_rot))):
        if abs(new_rot[i] - old_rot[i]) > ROTATION_UPDATE_THRESHOLD:
            return True
    
    return False

# Start background task on first connection
first_connect = True

@socketio.on('connect')
def handle_connect():
    """Handle new client connection."""
    global first_connect
    sid = request.sid # Store SID
    logger.info(f"Client connected: {sid}")

    # Send initial server status to the new client
    try:
        update_server_status() # Ensure status is current
        socketio.emit('serverStatus', server_status, room=sid)
        logger.info(f"Sent initial server status to {sid}")

        # Also send current player list to the new client (with capitalized teams)
        with game_state_lock:
            players_copy = get_players_for_client()
        if players_copy:
            logger.info(f"Sending initial player list to {sid}: {len(players_copy)} players")
            socketio.emit('players', players_copy, room=sid)
        else:
             logger.info(f"No players currently in game, not sending initial player list to {sid}")

    except Exception as e:
        logger.error(f"Error sending initial status/players to {sid}: {str(e)}")

    # Start background task *after* initial setup for the client
    if first_connect:
        try:
            # Check if it's truly the first connect within this worker process
            # This simple check might need refinement in multi-worker scenarios, but is okay for one worker.
            if first_connect: # Double check to avoid race condition if multiple connects happen quickly
                 socketio.start_background_task(background_task)
                 logger.info("Started background status broadcast task")
                 first_connect = False # Mark as started
        except Exception as e:
            logger.error(f"Failed to start background task: {str(e)}")
            first_connect = True # Reset flag on failure to allow retry

    # We don't need to add a placeholder for the SID here anymore,
    # as the join event handles adding the player properly.
    # logger.debug(f"Finished handling connect for {sid}")

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
    # Broadcast player list update with capitalized team names for client
    with game_state_lock:
        players_for_client = get_players_for_client()
    socketio.emit('players', players_for_client)

@socketio.on('join')
def handle_join(data):
    """Handle player join request."""
    server_stats['messages_received'] += 1
    start_time = time.time()

    player_name = data.get('name', f"Player_{uuid.uuid4().hex[:6]}")
    preferred_team = data.get('team', None)
    sid = request.sid

    logger.info(f"[handle_join] SID: {sid}, Name: {player_name}, Preferred team: {preferred_team}")

    join_success = False
    assigned_team = None
    spawn_position = None
    error_message = None

    try:
        with game_state_lock:
            if sid not in game_state['players']:
                # Assign team using balancing logic
                assigned_team = assign_team(preferred_team)

                # Calculate spawn position based on team
                base_pos = [0, 2, -110] if assigned_team == 'red' else [0, 2, 110]
                spawn_position = calculate_random_spawn(base_pos)

                game_state['players'][sid] = {
                    'name': player_name,
                    'team': assigned_team,
                    'position': spawn_position,
                    'rotation': [0, 0, 0],
                    'health': 100,
                    'kills': 0,
                    'deaths': 0,
                    'score': 0,
                    'is_eliminated': False,
                    'joinTime': time.time()
                }
                join_success = True
                logger.info(f"[handle_join] Player {player_name} added to team {assigned_team}. Total players: {len(game_state['players'])}")

                # Update server status immediately
                update_server_status()
            else:
                # Player already exists, just confirm
                player = game_state['players'][sid]
                assigned_team = player['team']
                spawn_position = player['position']
                join_success = True
                logger.info(f"[handle_join] Player {player_name} already in game on team {assigned_team}")

        # --- Lock released ---

        if join_success:
            # Convert team name to capitalized format for client compatibility
            # Server internally uses lowercase, client expects capitalized
            client_team = assigned_team.capitalize() if assigned_team else 'Red'

            # Send essential notifications
            socketio.emit('joinSuccess', {'team': client_team, 'position': spawn_position}, room=sid)
            socketio.emit('healthUpdate', {'health': 100}, room=sid)

            # Broadcast updated player list and status to all clients
            # Convert all player teams to capitalized for client
            players_for_client = {}
            with game_state_lock:
                for pid, pdata in game_state['players'].items():
                    players_for_client[pid] = {
                        **pdata,
                        'team': pdata['team'].capitalize() if pdata.get('team') else 'Red'
                    }
            socketio.emit('players', players_for_client)
            socketio.emit('serverStatus', server_status)

            logger.info(f"[handle_join] Notifications sent for {player_name} on team {client_team}")

    except Exception as e:
        logger.error(f"[handle_join] SID: {sid} - Exception: {str(e)}", exc_info=True)
        error_message = {'message': 'Error joining game'}

    finally:
        end_time = time.time()
        logger.info(f"[handle_join] SID: {sid} - Completed in {end_time - start_time:.4f} seconds")
        if error_message:
            socketio.emit('connectionError', error_message, room=sid)

@socketio.on('message')
def handle_message(data):
    """Handle chat messages."""
    server_stats['messages_received'] += 1
    
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
        server_stats['messages_sent'] += 1

@socketio.on('updatePosition')
def handle_update_position(data):
    """Handle player position updates with optimization."""
    server_stats['messages_received'] += 1
    server_stats['position_updates'] += 1
    
    with game_state_lock:
        if request.sid in game_state['players']:
            player = game_state['players'][request.sid]

            # Ignore updates if player is eliminated
            if player.get('is_eliminated', False):
                return

            # Update player position
            position = data.get('position')
            rotation = data.get('rotation')
            
            # Only process significant changes
            position_changed = False
            rotation_changed = False
            
            if position:
                if position_changed_significantly(position, player.get('lastPosition')):
                    player['position'] = position
                    player['lastPosition'] = position
                    position_changed = True
            
            if rotation:
                if rotation_changed_significantly(rotation, player.get('lastRotation')):
                    player['rotation'] = rotation
                    player['lastRotation'] = rotation
                    rotation_changed = True
            
            # Only check for interactions if position changed
            if position_changed:
                check_flag_interactions(request.sid, player)

@socketio.on('shoot')
def handle_shoot(data):
    """Handle player shooting."""
    server_stats['messages_received'] += 1
    
    with game_state_lock:
        if request.sid in game_state['players']:
            shooter = game_state['players'][request.sid]
            
            # Prevent shooting if eliminated
            if shooter.get('is_eliminated', False):
                return

            # Broadcast paintball to all players
            socketio.emit('paintball', {
                'id': f"pb_{time.time()}_{request.sid}",
                'origin': data['origin'],
                'direction': data['direction'],
                'color': '#ff4500' if shooter['team'] == 'red' else '#0066ff'
            }, broadcast=True)
            server_stats['messages_sent'] += 1

@socketio.on('hit')
def handle_hit(data):
    """Handle paintball hits."""
    server_stats['messages_received'] += 1
    
    with game_state_lock:
        target_id = data.get('target')
        shooter_id = data.get('shooter')

        # Validate IDs and ensure they are different players
        if not target_id or not shooter_id or target_id == shooter_id:
            return
        if target_id not in game_state['players'] or shooter_id not in game_state['players']:
            return

        target = game_state['players'][target_id]
        shooter = game_state['players'][shooter_id]

        # Ignore hits if target is already eliminated or friendly fire
        if target.get('is_eliminated', False) or target['team'] == shooter['team']:
            return

        # Apply damage
        target['health'] -= HIT_DAMAGE
        logger.info(f"Player {target['name']} hit by {shooter['name']}. Health: {target['health']}")

        # Broadcast health update specifically for the target
        socketio.emit('healthUpdate', {'health': target['health']}, room=target_id)

        # Check if player is eliminated
        if target['health'] <= 0:
            handle_player_elimination(target_id, shooter_id)
            # No need to broadcast game state here, elimination handles it

        # Optional: Broadcast minimal hit confirmation if needed
        # socketio.emit('playerHit', {'target': target_id, 'shooter': shooter_id, 'newHealth': target['health']})

def handle_player_elimination(target_id, shooter_id):
    """Handles player elimination, stats, flag drop, and starts respawn timer."""
    if target_id not in game_state['players'] or shooter_id not in game_state['players']:
        return # Should not happen if called from handle_hit, but safety check

    target = game_state['players'][target_id]
    shooter = game_state['players'][shooter_id]

    # Prevent double elimination processing
    if target.get('is_eliminated', False):
        return

    logger.info(f"Player {target['name']} eliminated by {shooter['name']}")

    # Mark as eliminated
    target['is_eliminated'] = True
    target['health'] = 0 # Ensure health is 0

    # Update stats
    target['deaths'] += 1
    shooter['kills'] += 1
    shooter['score'] += 1 # Or adjust scoring logic as needed

    # --- Flag Drop Logic ---
    flag_dropped = None
    # Check if target was carrying the RED flag
    if game_state['flags']['red']['carrier'] == target_id:
        game_state['flags']['red']['captured'] = False
        game_state['flags']['red']['carrier'] = None
        # The 'position' of the red flag remains its base position [0, 0, -110]
        flag_dropped = 'red'
        # Emit event to notify clients the flag is back at base
        socketio.emit('flagReturned', {'team': 'red'})
        logger.info(f"{target['name']} dropped the red flag. It returned to base.")

    # Check if target was carrying the BLUE flag
    if game_state['flags']['blue']['carrier'] == target_id:
        game_state['flags']['blue']['captured'] = False
        game_state['flags']['blue']['carrier'] = None
        # The 'position' of the blue flag remains its base position [0, 0, 110]
        flag_dropped = 'blue'
        # Emit event to notify clients the flag is back at base
        socketio.emit('flagReturned', {'team': 'blue'})
        logger.info(f"{target['name']} dropped the blue flag. It returned to base.")
    # --- End Flag Drop Logic ---

    # Emit kill event announcement to all players
    kill_info = {
        'victim': {'id': target_id, 'name': target['name'], 'team': target['team']},
        'killer': {'id': shooter_id, 'name': shooter['name'], 'team': shooter['team']},
        'timestamp': time.time()
    }
    socketio.emit('playerKilled', kill_info)
    server_stats['messages_sent'] += 1

    # Send updated stats to the involved players
    socketio.emit('statsUpdate', {'kills': shooter['kills'], 'score': shooter['score']}, room=shooter_id)
    socketio.emit('statsUpdate', {'deaths': target['deaths']}, room=target_id)

    # Start respawn timer for the target player
    socketio.emit('startRespawnTimer', {'duration': RESPAWN_TIME}, room=target_id)
    logger.info(f"Starting {RESPAWN_TIME}s respawn timer for {target['name']}")

    respawn_timer = Timer(RESPAWN_TIME, respawn_player, args=[target_id])
    respawn_timer.start()
    # target['respawn_timer'] = respawn_timer # Optional: Store timer

    # Broadcast updated game state (will include the reset flag status)
    broadcast_game_state()

def respawn_player(player_id):
    """Respawns a player after the timer."""
    with game_state_lock:
        if player_id not in game_state['players']:
            logger.warning(f"Attempted to respawn player {player_id} who is no longer in game.")
            return

        player = game_state['players'][player_id]

        # Ensure player is still marked as eliminated before respawning
        if not player.get('is_eliminated', False):
            logger.warning(f"Attempted to respawn player {player['name']} who is not eliminated.")
            return

        # Reset player state
        player['health'] = 100
        player['is_eliminated'] = False
        # player['respawn_timer'] = None # Clear timer reference if stored

        # Calculate new random spawn position
        base_pos = [0, 2, -110] if player['team'] == 'red' else [0, 2, 110]
        player['position'] = calculate_random_spawn(base_pos)
        player['lastPosition'] = None # Reset last known position
        player['lastRotation'] = None # Reset last known rotation

        logger.info(f"Player {player['name']} respawned at {player['position']}")

        # Notify the player they have respawned
        respawn_data = {
            'message': 'You have respawned!',
            'position': player['position'],
            'health': player['health']
        }
        socketio.emit('playerRespawned', respawn_data, room=player_id)

        # Broadcast updated game state (includes new position and status)
        broadcast_game_state()

def check_flag_interactions(player_sid, player):
    """Check and handle player interactions with flags."""
    # Ignore interactions if player is eliminated
    if player.get('is_eliminated', False):
        return

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
            server_stats['messages_sent'] += 1
    
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
            server_stats['messages_sent'] += 1
            
            # Check win condition
            if game_state['scores'][player['team']] >= WIN_SCORE:
                socketio.emit('gameOver', {
                    'winner': player['team'],
                    'redScore': game_state['scores']['red'],
                    'blueScore': game_state['scores']['blue']
                })
                server_stats['messages_sent'] += 1
                
                # Reset scores for next game
                game_state['scores']['red'] = 0
                game_state['scores']['blue'] = 0

@app.route('/status')
def server_status_endpoint():
    """API endpoint to get server status."""
    update_server_status()
    return jsonify(server_status)

@app.route('/performance')
def server_performance_endpoint():
    """API endpoint to get server performance statistics."""
    uptime = time.time() - server_stats['start_time']
    
    # Calculate rates
    messages_per_second = server_stats['messages_received'] / uptime if uptime > 0 else 0
    position_updates_per_second = server_stats['position_updates'] / uptime if uptime > 0 else 0
    
    return jsonify({
        'uptime': int(uptime),
        'messages_received': server_stats['messages_received'],
        'messages_sent': server_stats['messages_sent'],
        'position_updates': server_stats['position_updates'],
        'messages_per_second': round(messages_per_second, 2),
        'position_updates_per_second': round(position_updates_per_second, 2),
        'queue_length': len(game_state['queue']),
        'player_count': len(game_state['players']),
    })

@socketio.on('requestServerStatus')
def handle_server_status_request():
    """Handle client request for server status."""
    server_stats['messages_received'] += 1
    update_server_status()
    socketio.emit('serverStatus', server_status, room=request.sid)
    server_stats['messages_sent'] += 1

# --- NEW: Helper function for randomized spawn ---
def calculate_random_spawn(base_pos):
    """Calculates a random position within a radius of the base."""
    angle = random.uniform(0, 2 * math.pi)
    radius = random.uniform(0, BASE_SPAWN_RADIUS)
    offset_x = radius * math.cos(angle)
    offset_z = radius * math.sin(angle)
    return [base_pos[0] + offset_x, base_pos[1], base_pos[2] + offset_z]

# Update the background task function to handle all player updates centrally
def background_task():
    """Background task to broadcast server status and player information periodically."""
    iteration = 0
    max_iterations = 86400  # 24 hours at 1 sec per iteration

    try:
        logger.info("Starting background task")

        while iteration < max_iterations:
            # Data to be emitted, prepared inside the lock
            current_server_status = None
            current_players_state = None
            should_broadcast_players = False

            try:
                # Update counters
                iteration += 1

                # Acquire lock, prepare data, release lock
                with game_state_lock:
                    # Always update status
                    update_server_status()
                    current_server_status = server_status.copy()
                    logger.debug(f"Background task status: {current_server_status.get('currentPlayers', 0)} players")

                    # Prepare player list broadcast every 2 seconds
                    if iteration % 2 == 0:
                        if game_state['players']:
                            current_players_state = get_players_for_client()
                            should_broadcast_players = True

                # --- Lock is released here ---

                # Perform emits outside the lock
                if current_server_status:
                    socketio.emit('serverStatus', current_server_status)
                    server_stats['messages_sent'] += 1

                if should_broadcast_players and current_players_state is not None:
                    try:
                        socketio.emit('players', current_players_state)
                        logger.debug(f"Broadcasting {len(current_players_state)} players")
                        server_stats['messages_sent'] += 1 # Increment only if players were actually sent
                    except Exception as e:
                        logger.error(f"Error broadcasting players: {str(e)}")

                # Sleep for 1 second between iterations, yielding control
                socketio.sleep(1)

            except Exception as e:
                logger.error(f"Error in background task iteration: {str(e)}")
                socketio.sleep(1) # Sleep after error

        logger.warning("Background task completed max iterations, restarting")
        socketio.start_background_task(background_task)

    except Exception as e:
        logger.error(f"Fatal error in background task, attempting restart: {str(e)}")
        socketio.sleep(5)
        socketio.start_background_task(background_task)

# Run the SocketIO server if executed directly
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    # Use different debug settings based on environment
    debug_mode = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    # Log startup information
    logger.info(f"Starting PaintBlast server on port {port}, debug={debug_mode}")
    logger.info(f"Using async_mode: {socketio.async_mode}")
    
    try:
        # Run with eventlet for production-like behavior even in development
        socketio.run(app, host='0.0.0.0', port=port, debug=debug_mode)
    except Exception as e:
        logger.critical(f"Failed to start server: {str(e)}") 