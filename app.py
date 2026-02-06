import eventlet
eventlet.monkey_patch()

import os
from flask import Flask, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
import math
import time
from threading import RLock, Timer
import logging
import uuid
from collections import deque
import json
import random

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('paintblast')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key')

# CORS Configuration
cors_origins = [
    "http://localhost:3000",
    "https://paintblast.vercel.app",
    "https://paintblast.luke-mp.co",
    "https://paintblast.lukemp.co",
    "https://paintblast-server.onrender.com",
    "https://paintblast-git-main-luk3mps-projects.vercel.app",
    "https://paintblast-luk3mps-projects.vercel.app",
]

# Set up SocketIO — disable verbose logging in production
is_debug = os.environ.get('DEBUG', 'False').lower() == 'true'
socketio = SocketIO(
    app, 
    cors_allowed_origins=cors_origins, 
    async_mode='eventlet',
    ping_timeout=60,
    ping_interval=25,
    manage_session=False,
    logger=is_debug,
    engineio_logger=is_debug,
    max_http_buffer_size=1e8,
    always_connect=True
)

# Apply CORS to regular HTTP routes
CORS(app, origins=cors_origins, supports_credentials=True)

# ========================================================================
#  GAME STATE
# ========================================================================

game_state = {
    'players': {},
    'status': 'waiting',
    'scores': {'red': 0, 'blue': 0},
    'flags': {
        'red': {'captured': False, 'carrier': None, 'position': [0, 0, -120]},
        'blue': {'captured': False, 'carrier': None, 'position': [0, 0, 120]}
    },
    'queue': deque()
}

server_status = {
    'currentPlayers': 0,
    'maxPlayers': 100,
    'queueLength': 0,
    'redTeamPlayers': 0,
    'blueTeamPlayers': 0
}

# Thread safety — reentrant lock for nested calls
game_state_lock = RLock()

# ========================================================================
#  CONSTANTS
# ========================================================================

POSITION_UPDATE_THRESHOLD = 0.1   # Lower threshold for smoother remote movement
ROTATION_UPDATE_THRESHOLD = 0.05  # Lower threshold for smoother remote aiming
FLAG_CAPTURE_RADIUS = 5
FLAG_SCORE_POINTS = 1
MIN_PLAYERS_PER_TEAM = 1
WIN_SCORE = 3
MAX_PLAYERS = 100
MAX_PLAYERS_PER_TEAM = 50
BASE_SPAWN_RADIUS = 5
HIT_DAMAGE = 34          # 3 hits to eliminate (34 * 3 = 102 > 100)
RESPAWN_TIME = 10         # 10-second respawn timer

# ========================================================================
#  HELPERS
# ========================================================================

def calculate_random_spawn(base_pos):
    """Random position within a radius of the base."""
    angle = random.uniform(0, 2 * math.pi)
    radius = random.uniform(0, BASE_SPAWN_RADIUS)
    return [
        base_pos[0] + radius * math.cos(angle),
        base_pos[1],
        base_pos[2] + radius * math.sin(angle)
    ]

def update_server_status():
    """Update server_status dict. Must be called with game_state_lock held."""
    server_status['currentPlayers'] = len(game_state['players'])
    server_status['queueLength'] = len(game_state['queue'])
    red_count = sum(1 for p in game_state['players'].values() if p['team'] == 'red')
    blue_count = sum(1 for p in game_state['players'].values() if p['team'] == 'blue')
    server_status['redTeamPlayers'] = red_count
    server_status['blueTeamPlayers'] = blue_count

def get_players_for_client():
    """Get player data with capitalized team names for client. Lock must be held."""
    result = {}
    for pid, pdata in game_state['players'].items():
        result[pid] = {
            **pdata,
            'team': pdata['team'].capitalize() if pdata.get('team') else 'Red'
        }
    return result

def assign_team(preferred_team=None):
    """Assign a team considering balance. Lock must be held."""
    red_count = sum(1 for p in game_state['players'].values() if p['team'] == 'red')
    blue_count = sum(1 for p in game_state['players'].values() if p['team'] == 'blue')

    normalized_pref = preferred_team.lower() if preferred_team else None

    if abs(red_count - blue_count) > 1:
        return 'red' if red_count < blue_count else 'blue'

    if normalized_pref in ['red', 'blue']:
        if normalized_pref == 'red' and red_count <= blue_count:
            return 'red'
        elif normalized_pref == 'blue' and blue_count <= red_count:
            return 'blue'

    return 'red' if red_count <= blue_count else 'blue'

def position_changed_significantly(new_pos, old_pos):
    if not old_pos:
        return True
    dx = new_pos[0] - old_pos[0]
    dy = new_pos[1] - old_pos[1]
    dz = new_pos[2] - old_pos[2]
    return (dx * dx + dy * dy + dz * dz) > POSITION_UPDATE_THRESHOLD ** 2

def rotation_changed_significantly(new_rot, old_rot):
    if not old_rot:
        return True
    for i in range(min(len(new_rot), len(old_rot))):
        if abs(new_rot[i] - old_rot[i]) > ROTATION_UPDATE_THRESHOLD:
            return True
    return False

def process_queue():
    """Process the queue. Lock must be held. Returns list of emits to fire outside lock."""
    pending_emits = []

    while len(game_state['players']) < MAX_PLAYERS and game_state['queue']:
        queued = game_state['queue'].popleft()
        sid = queued['sid']
        player_name = queued['name']
        preferred_team = queued['team']

        assigned_team = assign_team(preferred_team)
        base_pos = [0, 2, -120] if assigned_team == 'red' else [0, 2, 120]
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

        client_team = assigned_team.capitalize()
        pending_emits.append({'event': 'joinSuccess', 'data': {'team': client_team, 'position': spawn_position}, 'kwargs': {'room': sid}})
        pending_emits.append({'event': 'healthUpdate', 'data': {'health': 100}, 'kwargs': {'room': sid}})

        logger.info(f"Player {player_name} joined from queue to team {assigned_team}")

    # Queue position updates
    for i, player_in_queue in enumerate(game_state['queue']):
        pending_emits.append({
            'event': 'queueUpdate',
            'data': {'position': i + 1, 'estimatedWaitTime': (i + 1) * 30},
            'kwargs': {'room': player_in_queue['sid']}
        })

    # Updated player list and server status
    update_server_status()
    pending_emits.append({'event': 'players', 'data': get_players_for_client()})
    pending_emits.append({'event': 'serverStatus', 'data': server_status.copy()})

    return pending_emits

def check_flag_interactions(player_sid, player):
    """Check flag capture/score. Lock must be held. Returns list of emits."""
    pending_emits = []

    if player.get('is_eliminated', False):
        return pending_emits

    if player['team'] == 'red':
        flag_team = 'blue'
        home_base = [0, 0, -120]
    else:
        flag_team = 'red'
        home_base = [0, 0, 120]

    player_pos = player['position']
    enemy_flag = game_state['flags'][flag_team]

    # Check capture
    if not enemy_flag['captured']:
        flag_pos = enemy_flag['position']
        dist = math.sqrt(
            (player_pos[0] - flag_pos[0]) ** 2 +
            (player_pos[1] - flag_pos[1]) ** 2 +
            (player_pos[2] - flag_pos[2]) ** 2
        )
        if dist < FLAG_CAPTURE_RADIUS:
            enemy_flag['captured'] = True
            enemy_flag['carrier'] = player_sid
            logger.info(f"Player {player['name']} captured the {flag_team} flag!")
            pending_emits.append({
                'event': 'flagCaptured',
                'data': {'team': flag_team, 'carrier': player['name']}
            })

    # Check scoring
    if game_state['flags'][flag_team]['carrier'] == player_sid:
        dist_home = math.sqrt(
            (player_pos[0] - home_base[0]) ** 2 +
            (player_pos[1] - home_base[1]) ** 2 +
            (player_pos[2] - home_base[2]) ** 2
        )
        if dist_home < FLAG_CAPTURE_RADIUS:
            game_state['flags'][flag_team]['captured'] = False
            game_state['flags'][flag_team]['carrier'] = None
            game_state['scores'][player['team']] += FLAG_SCORE_POINTS

            logger.info(f"Player {player['name']} scored with {flag_team} flag! Scores: R{game_state['scores']['red']} B{game_state['scores']['blue']}")

            pending_emits.append({
                'event': 'flagScored',
                'data': {
                    'team': flag_team,
                    'scorer': player['name'],
                    'redScore': game_state['scores']['red'],
                    'blueScore': game_state['scores']['blue']
                }
            })

            if game_state['scores'][player['team']] >= WIN_SCORE:
                pending_emits.append({
                    'event': 'gameOver',
                    'data': {
                        'winner': player['team'],
                        'redScore': game_state['scores']['red'],
                        'blueScore': game_state['scores']['blue']
                    }
                })
                game_state['scores']['red'] = 0
                game_state['scores']['blue'] = 0

    return pending_emits

def handle_player_elimination(target_id, shooter_id):
    """Handle elimination. Lock must be held. Returns list of emits."""
    pending_emits = []

    if target_id not in game_state['players'] or shooter_id not in game_state['players']:
        return pending_emits

    target = game_state['players'][target_id]
    shooter = game_state['players'][shooter_id]

    if target.get('is_eliminated', False):
        return pending_emits

    logger.info(f"Player {target['name']} eliminated by {shooter['name']}")

    target['is_eliminated'] = True
    target['health'] = 0
    target['deaths'] += 1
    shooter['kills'] += 1
    shooter['score'] += 1

    # Flag drop
    for flag_team in ['red', 'blue']:
        if game_state['flags'][flag_team]['carrier'] == target_id:
            game_state['flags'][flag_team]['captured'] = False
            game_state['flags'][flag_team]['carrier'] = None
            pending_emits.append({'event': 'flagReturned', 'data': {'team': flag_team}})
            logger.info(f"{target['name']} dropped the {flag_team} flag")

    pending_emits.append({
        'event': 'playerKilled',
        'data': {
            'victim': {'id': target_id, 'name': target['name'], 'team': target['team']},
            'killer': {'id': shooter_id, 'name': shooter['name'], 'team': shooter['team']},
            'timestamp': time.time()
        }
    })
    pending_emits.append({
        'event': 'statsUpdate',
        'data': {'kills': shooter['kills'], 'score': shooter['score']},
        'kwargs': {'room': shooter_id}
    })
    pending_emits.append({
        'event': 'statsUpdate',
        'data': {'deaths': target['deaths']},
        'kwargs': {'room': target_id}
    })
    pending_emits.append({
        'event': 'startRespawnTimer',
        'data': {'duration': RESPAWN_TIME},
        'kwargs': {'room': target_id}
    })

    return pending_emits

def respawn_player(player_id):
    """Respawn a player after timer. Called from a Timer thread."""
    respawn_data = None
    players_data = None

    with game_state_lock:
        if player_id not in game_state['players']:
            return

        player = game_state['players'][player_id]
        if not player.get('is_eliminated', False):
            return

        player['health'] = 100
        player['is_eliminated'] = False
        base_pos = [0, 2, -120] if player['team'] == 'red' else [0, 2, 120]
        player['position'] = calculate_random_spawn(base_pos)
        player['lastPosition'] = None
        player['lastRotation'] = None

        logger.info(f"Player {player['name']} respawned at {player['position']}")

        respawn_data = {
            'message': 'You have respawned!',
            'position': list(player['position']),
            'health': player['health']
        }
        players_data = get_players_for_client()

    # Emit outside lock
    if respawn_data:
        socketio.emit('playerRespawned', respawn_data, room=player_id)
    if players_data:
        socketio.emit('players', players_data)

# ========================================================================
#  SOCKET EVENT HANDLERS
# ========================================================================

first_connect = True

@socketio.on('connect')
def handle_connect():
    global first_connect
    sid = request.sid
    logger.info(f"Client connected: {sid}")

    # Send initial state
        with game_state_lock:
        update_server_status()
        status_copy = server_status.copy()
        players_copy = get_players_for_client()

    try:
        socketio.emit('serverStatus', status_copy, room=sid)
        if players_copy:
            socketio.emit('players', players_copy, room=sid)
    except Exception as e:
        logger.error(f"Error sending initial state to {sid}: {e}")

    # Start background task on first connection
    if first_connect:
        try:
                 socketio.start_background_task(background_task)
            logger.info("Started background broadcast task")
            first_connect = False
        except Exception as e:
            logger.error(f"Failed to start background task: {e}")


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    logger.info(f"Client disconnected: {sid}")

    pending_emits = []
    
    with game_state_lock:
        if sid in game_state['players']:
            player = game_state['players'].pop(sid)
            logger.info(f"Player {player['name']} left the game. Remaining: {len(game_state['players'])}")

            # Drop flags
            for flag_team in ['red', 'blue']:
                if game_state['flags'][flag_team]['carrier'] == sid:
                    game_state['flags'][flag_team]['captured'] = False
                    game_state['flags'][flag_team]['carrier'] = None
                    pending_emits.append({'event': 'flagReturned', 'data': {'team': flag_team}})

            # Process queue (returns more emits)
            queue_emits = process_queue()
            pending_emits.extend(queue_emits)

        else:
            # Check queue
            for i, player_in_queue in enumerate(game_state['queue']):
                if player_in_queue['sid'] == sid:
                    game_state['queue'].remove(player_in_queue)
                    logger.info(f"Player {player_in_queue['name']} left the queue")
                    break
    
        update_server_status()
        status_copy = server_status.copy()
        players_copy = get_players_for_client()

    # Emit everything outside lock
    for evt in pending_emits:
        socketio.emit(evt['event'], evt['data'], **evt.get('kwargs', {}))

    socketio.emit('serverStatus', status_copy)
    socketio.emit('players', players_copy)


@socketio.on('join')
def handle_join(data):
    player_name = data.get('name', f"Player_{uuid.uuid4().hex[:6]}")
    preferred_team = data.get('team', None)
    sid = request.sid

    logger.info(f"[join] SID: {sid}, Name: {player_name}, Preferred: {preferred_team}")

    join_success = False
    assigned_team = None
    spawn_position = None
    join_data = None
    players_data = None
    status_data = None
    queue_pos_data = None

        with game_state_lock:
        if sid in game_state['players']:
            # Already in game — just re-confirm (handles reconnection)
            player = game_state['players'][sid]
            assigned_team = player['team']
            spawn_position = player['position']
            join_success = True
            logger.info(f"[join] Player {player_name} already in game on team {assigned_team}")
        elif len(game_state['players']) < MAX_PLAYERS:
            # Join directly
            assigned_team = assign_team(preferred_team)
            base_pos = [0, 2, -120] if assigned_team == 'red' else [0, 2, 120]
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
                update_server_status()
            logger.info(f"[join] Player {player_name} added to team {assigned_team}. Total: {len(game_state['players'])}")
            else:
            # Server full — add to queue
            game_state['queue'].append({
                'sid': sid,
                'name': player_name,
                'team': preferred_team
            })
            queue_pos = len(game_state['queue'])
            logger.info(f"[join] Server full. Player {player_name} added to queue at position {queue_pos}")

        # Prepare data for emits
        if join_success:
            client_team = assigned_team.capitalize()
            join_data = {'team': client_team, 'position': spawn_position}
            players_data = get_players_for_client()
            status_data = server_status.copy()
        else:
            queue_pos_data = {
                'position': len(game_state['queue']),
                'estimatedWaitTime': len(game_state['queue']) * 30
            }

    # Emit outside lock
        if join_success:
        socketio.emit('joinSuccess', join_data, room=sid)
            socketio.emit('healthUpdate', {'health': 100}, room=sid)
        socketio.emit('players', players_data)
        socketio.emit('serverStatus', status_data)
    else:
        socketio.emit('queueUpdate', queue_pos_data, room=sid)


@socketio.on('updatePosition')
def handle_update_position(data):
    pending_emits = []
    
    with game_state_lock:
        if request.sid not in game_state['players']:
            return

            player = game_state['players'][request.sid]

            if player.get('is_eliminated', False):
                return

            position = data.get('position')
            rotation = data.get('rotation')
        is_crouching = data.get('isCrouching', False)
            
            position_changed = False
            
        if position and position_changed_significantly(position, player.get('lastPosition')):
                    player['position'] = position
                    player['lastPosition'] = position
                    position_changed = True
            
        if rotation and rotation_changed_significantly(rotation, player.get('lastRotation')):
                    player['rotation'] = rotation
                    player['lastRotation'] = rotation
            
        # Always update crouch state
        player['isCrouching'] = is_crouching

            if position_changed:
            pending_emits = check_flag_interactions(request.sid, player)

    # Emit outside lock
    for evt in pending_emits:
        socketio.emit(evt['event'], evt['data'], **evt.get('kwargs', {}))


@socketio.on('shoot')
def handle_shoot(data):
    paintball_data = None
    
    with game_state_lock:
        if request.sid not in game_state['players']:
            return
            
        shooter = game_state['players'][request.sid]
            if shooter.get('is_eliminated', False):
                return

        paintball_data = {
                'id': f"pb_{time.time()}_{request.sid}",
                'origin': data['origin'],
                'direction': data['direction'],
            'color': '#ff4500' if shooter['team'] == 'red' else '#0066ff',
            'shooter': request.sid
        }

    if paintball_data:
        emit('paintball', paintball_data, broadcast=True, include_self=False)


@socketio.on('hit')
def handle_hit(data):
        target_id = data.get('target')
        shooter_id = data.get('shooter')

    pending_emits = []

    with game_state_lock:
        if not target_id or not shooter_id or target_id == shooter_id:
            return
        if target_id not in game_state['players'] or shooter_id not in game_state['players']:
            return

        target = game_state['players'][target_id]
        shooter = game_state['players'][shooter_id]

        if target.get('is_eliminated', False) or target['team'] == shooter['team']:
            return

        target['health'] -= HIT_DAMAGE
        logger.info(f"Player {target['name']} hit by {shooter['name']}. Health: {target['health']}")

        pending_emits.append({
            'event': 'healthUpdate',
            'data': {'health': target['health']},
            'kwargs': {'room': target_id}
        })
        # Notify the shooter that their hit was confirmed
        pending_emits.append({
            'event': 'hitConfirmed',
            'data': {'targetId': target_id, 'shooterId': shooter_id},
            'kwargs': {'room': shooter_id}
        })

        if target['health'] <= 0:
            elim_emits = handle_player_elimination(target_id, shooter_id)
            pending_emits.extend(elim_emits)

    # Emit outside lock
    for evt in pending_emits:
        socketio.emit(evt['event'], evt['data'], **evt.get('kwargs', {}))

    # Start respawn timer outside lock
    if any(e['event'] == 'startRespawnTimer' for e in pending_emits):
        timer = Timer(RESPAWN_TIME, respawn_player, args=[target_id])
        timer.start()


@socketio.on('captureFlag')
def handle_capture_flag(data):
    flag_team = data.get('team', '').lower()
    if flag_team not in ('red', 'blue'):
        return

    capture_emit = None

    with game_state_lock:
        if request.sid not in game_state['players']:
            return
        player = game_state['players'][request.sid]
    if player.get('is_eliminated', False):
        return
        if player['team'] == flag_team:
            return

    enemy_flag = game_state['flags'][flag_team]
    if not enemy_flag['captured']:
            enemy_flag['captured'] = True
            enemy_flag['carrier'] = request.sid
            logger.info(f"[captureFlag] Player {player['name']} captured {flag_team} flag!")
            capture_emit = {'team': flag_team, 'carrier': player['name']}

    if capture_emit:
        socketio.emit('flagCaptured', capture_emit)


@socketio.on('scoreFlag')
def handle_score_flag(data):
    flag_team = data.get('team', '').lower()
    if flag_team not in ('red', 'blue'):
        return

    pending_emits = []

    with game_state_lock:
        if request.sid not in game_state['players']:
            return
        player = game_state['players'][request.sid]
        if player.get('is_eliminated', False):
            return
        if game_state['flags'][flag_team]['carrier'] != request.sid:
            return

            game_state['flags'][flag_team]['captured'] = False
            game_state['flags'][flag_team]['carrier'] = None
            game_state['scores'][player['team']] += FLAG_SCORE_POINTS
            
        logger.info(f"[scoreFlag] Player {player['name']} scored with {flag_team} flag! R{game_state['scores']['red']} B{game_state['scores']['blue']}")
            
        pending_emits.append({
            'event': 'flagScored',
            'data': {
                'team': flag_team,
                'scorer': player['name'],
                'redScore': game_state['scores']['red'],
                'blueScore': game_state['scores']['blue']
            }
            })
            
            if game_state['scores'][player['team']] >= WIN_SCORE:
            pending_emits.append({
                'event': 'gameOver',
                'data': {
                    'winner': player['team'],
                    'redScore': game_state['scores']['red'],
                    'blueScore': game_state['scores']['blue']
                }
                })
                game_state['scores']['red'] = 0
                game_state['scores']['blue'] = 0

    for evt in pending_emits:
        socketio.emit(evt['event'], evt['data'])


@socketio.on('message')
def handle_message(data):
    message = None
    with game_state_lock:
        if request.sid in game_state['players']:
            player = game_state['players'][request.sid]
            message = {
                'sender': player['name'],
                'team': player['team'],
                'text': data.get('text', ''),
                'timestamp': data.get('timestamp', time.time())
            }

    if message:
        socketio.emit('message', message)


@socketio.on('requestServerStatus')
def handle_server_status_request():
    with game_state_lock:
        update_server_status()
        status_copy = server_status.copy()
    socketio.emit('serverStatus', status_copy, room=request.sid)


# ========================================================================
#  HTTP ENDPOINTS
# ========================================================================

@app.route('/status')
def server_status_endpoint():
    with game_state_lock:
    update_server_status()
        return jsonify(server_status.copy())

@app.route('/performance')
def server_performance_endpoint():
    with game_state_lock:
        player_count = len(game_state['players'])
        queue_length = len(game_state['queue'])
    return jsonify({
        'player_count': player_count,
        'queue_length': queue_length,
    })

@app.route('/health')
def health_check():
    """Simple health check endpoint for Render."""
    return jsonify({'status': 'ok'})


# ========================================================================
#  BACKGROUND TASK
# ========================================================================

def background_task():
    """Periodically broadcast server status and player positions."""
    iteration = 0
    max_iterations = 864000  # 24 hours at 100ms intervals

    try:
        logger.info("Background broadcast task started")

        while iteration < max_iterations:
            try:
                iteration += 1

                with game_state_lock:
                    player_count = len(game_state['players'])

                    # Always prepare player data if there are players
                    players_data = None
                    if player_count > 0:
                        players_data = get_players_for_client()

                    # Update server status less frequently (every 50 ticks = 5s)
                    status_copy = None
                    if iteration % 50 == 0:
                    update_server_status()
                        status_copy = server_status.copy()

                # Emit outside lock — players every 100ms (10 ticks/sec)
                if players_data is not None:
                    socketio.emit('players', players_data)

                if status_copy is not None:
                    socketio.emit('serverStatus', status_copy)

                socketio.sleep(0.1)

                    except Exception as e:
                logger.error(f"Error in background task: {e}")
                socketio.sleep(1)

        logger.warning("Background task completed max iterations, restarting")
        socketio.start_background_task(background_task)

    except Exception as e:
        logger.error(f"Fatal error in background task: {e}")
        socketio.sleep(5)
        socketio.start_background_task(background_task)


# ========================================================================
#  MAIN
# ========================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    debug_mode = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Starting PaintBlast server on port {port}, debug={debug_mode}")
    
    try:
        socketio.run(app, host='0.0.0.0', port=port, debug=debug_mode)
    except Exception as e:
        logger.critical(f"Failed to start server: {e}")
