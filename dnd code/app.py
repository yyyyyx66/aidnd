from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
from flask_socketio import SocketIO, emit, join_room, leave_room
import requests
import json
from datetime import datetime
import socket
import time
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'  # 用于session加密
socketio = SocketIO(app, 
    ping_timeout=60,  # 增加 ping 超时时间
    ping_interval=25,  # 减少 ping 间隔
    cors_allowed_origins="*",  # 允许跨域
    async_mode='threading'  # 使用线程模式
)

# Deepseek API 配置
API_KEY = "sk-fc2c812eeadf4ed98c1a419f94ee44a8"  # 替换为你的 Deepseek API Key

# 创建 OpenAI 客户端
client = OpenAI(
    api_key=API_KEY,
    base_url="https://api.deepseek.com/v1"  # 确认这是正确的 API 地址
)

# 添加重试和超时配置
API_CONFIG = {
    'max_retries': 3,           # 最大重试次数
    'retry_delay': 5,           # 重试间隔（秒）
    'connect_timeout': 10,      # 连接超时时间
    'read_timeout': 300,        # 读取超时时间
    'verify_ssl': False,        # 是否验证SSL证书
    'proxies': None            # 如果需要代理，在这里配置
}

# 内存存储
rooms = {}
users = {}

# 添加游戏状态常量
GAME_STATES = {
    'WAITING': 'waiting',    # 等待开始
    'CREATING': 'creating',  # 角色创建阶段
    'PLAYING': 'playing',    # 游戏进行中
    'SCENE': 'scene'         # 场景描述阶段
}

SYSTEM_PROMPT = """
你是一位经验丰富的DND地下城主。你需要：
1. 帮助玩家创建角色
2. 引导故事发展
3. 处理玩家的行动和决策
4. 进行战斗判定
5. 维持游戏规则和平衡

请用生动有趣的方式描述场景，让玩家感受到身临其境的体验。
在回复时，请使用Markdown格式来优化文本显示：
- 使用 **粗体** 强调重要信息
- 使用 > 引用块来描述场景
- 使用 ### 等标题来区分不同部分
- 使用 * 或 - 来创建列表
- 使用 ``` 来标注规则说明

保持文字生动有趣，同时结构清晰。
"""

# 在 SYSTEM_PROMPT 后面添加
CHARACTER_TEMPLATE = """请严格按照以下格式生成每个角色，角色之间使用"---分隔线---"分隔：

选项 [序号]：
1. 名字：[富有特色的角色名字]
2. 种族：[种族名称] - [详细的种族特点描述]
3. 职业：[职业名称] [等级] - [详细的专精方向描述]
4. 属性值：
   力量：[10-18] | 敏捷：[10-18] | 体质：[10-18]
   智力：[10-18] | 感知：[10-18] | 魅力：[10-18]
5. 性格：[详细的性格描述，包括优点和缺点]
6. 背景故事：[详细的个人历史，与世界观相连]
7. 技能专长：
    - [主要技能1及其应用场景]
    - [主要技能2及其应用场景]
    - [主要技能3及其应用场景]
    - [特色专长及其效果]
8. 装备：
    - 武器：[主要武器及其特点]
    - 防具：[防具类型及其特点]
    - 其他：[特色装备及其用途]
9. 动机：[详细的冒险动机，包括个人目标和愿望]

---分隔线---

[继续生成下一个完全不同的角色]"""

# 添加进度跟踪
progress_queues = {}

def generate_progress_events(queue_id):
    """生成进度事件"""
    q = progress_queues[queue_id]
    while True:
        try:
            progress = q.get(timeout=1)
            if progress == 'DONE':
                yield f"data: {{'progress': 100, 'status': 'done'}}\n\n"
                break
            yield f"data: {{'progress': {progress}, 'status': 'processing'}}\n\n"
        except queue.Empty:
            continue

@app.route('/')
def home():
    if 'user_id' not in session:
        return render_template('login.html')
    return redirect(url_for('lobby'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        if username:
            # 生成唯一的用户ID
            user_id = f"user_{len(users) + 1}"  # 使用字符串格式的ID
            users[user_id] = {
                'username': username,
                'role': None,
                'room': None
            }
            session['user_id'] = user_id
            return redirect(url_for('lobby'))
    return render_template('login.html')

@app.route('/lobby')
def lobby():
    if 'user_id' not in session:
        return redirect(url_for('home'))
    return render_template('lobby.html', rooms=rooms)

@app.route('/game/<room_id>')
def game(room_id):
    if 'user_id' not in session:
        return redirect(url_for('home'))
    if room_id not in rooms:
        return redirect(url_for('lobby'))
    
    user_id = session['user_id']
    if user_id not in users:  # 添加用户检查
        session.clear()
        return redirect(url_for('home'))
        
    user = users[user_id]
    room = rooms[room_id]
    
    if user_id not in room['players'] and user_id != room['dm']:
        return redirect(url_for('lobby'))
        
    return render_template('game.html', 
                         room=room, 
                         user=user, 
                         is_dm=(user_id == room['dm']))

@app.route('/create_room', methods=['POST'])
def create_room():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': '请先登录'})
    
    room_name = request.form.get('room_name')
    if not room_name:
        return jsonify({'success': False, 'error': '房间名不能为空'})
    
    room_id = f"room_{len(rooms) + 1}"
    rooms[room_id] = {
        'id': room_id,
        'name': room_name,
        'dm': None,
        'players': [],
        'messages': [],
        'state': GAME_STATES['WAITING'],  # 添加游戏状态
        'character_options': {},          # 存储每个玩家的角色选项
        'selected_characters': {}         # 存储玩家选择的角色
    }
    return jsonify({'success': True, 'room_id': room_id})

@app.route('/rooms')
def get_rooms():
    return jsonify(rooms)

@socketio.on('join_room')
def on_join_room(data):
    room_id = data.get('room_id')
    role = data.get('role')
    
    if 'user_id' in session and room_id in rooms:
        user_id = session['user_id']
        room = rooms[room_id]
        user = users[user_id]
        
        # 先加入房间
        join_room(room_id)
        join_room(user_id)
        
        # 检查是否是新玩家
        is_new_player = False
        if role == 'dm' and not room['dm']:
            room['dm'] = user_id
            user['role'] = 'dm'
            is_new_player = True
        elif role == 'player' and user_id not in room['players']:
            room['players'].append(user_id)
            user['role'] = 'player'
            is_new_player = True
        
        user['room'] = room_id
        
        # 无论是否是新玩家，都发送这些消息给自己
        # 1. 发送欢迎消息
        welcome_msg = {
            'user': 'System',
            'content': f'欢迎 {user["username"]} 加入游戏！当前玩家数：{len(room["players"])}人',
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', welcome_msg, room=user_id)
        
        # 2. 发送游戏状态
        status_msg = {
            'user': 'System',
            'content': get_game_status(room, user),
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', status_msg, room=user_id)
        
        # 如果是新玩家，给其他人发送通知
        if is_new_player:
            # 给其他玩家发送新玩家加入的消息
            join_msg = {
                'user': 'System',
                'content': f'玩家 {user["username"]} 加入了游戏！当前玩家数：{len(room["players"])}人',
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            # 给其他玩家发送消息
            for player_id in room['players']:
                if player_id != user_id:  # 不给新玩家自己发送
                    emit('new_message', join_msg, room=player_id)
            if room['dm'] and room['dm'] != user_id:  # 给DM发送（如果DM不是新玩家）
                emit('new_message', join_msg, room=room['dm'])

@socketio.on('message')
def handle_message(data):
    if 'user_id' not in session:
        return
    
    user_id = session['user_id']
    user = users[user_id]
    message = data.get('message')
    room_id = data.get('room_id')
    
    if not all([message, room_id]) or room_id not in rooms:
        return
    
    room = rooms[room_id]
    
    # DM的命令处理
    if user['role'] == 'dm':
        # 开始游戏命令
        if message.strip().lower() == '/start':
            if room['state'] == GAME_STATES['WAITING']:
                try:
                    # 检查是否有足够的玩家
                    if len(room['players']) < 1:
                        raise Exception("至少需要1名玩家才能开始游戏")
                    
                    # 发送开始游戏通知
                    start_msg = {
                        'user': 'System',
                        'content': 'DM开始了游戏，正在生成世界背景和角色选项...',
                        'timestamp': datetime.now().strftime('%H:%M:%S')
                    }
                    emit('new_message', start_msg, room=room['id'], broadcast=True)
                    
                    # 更新游戏状态
                    room['state'] = GAME_STATES['CREATING']
                    
                    # 生成角色选项
                    generate_character_options(room)
                    
                except Exception as e:
                    error_msg = {
                        'user': 'System',
                        'content': f'开始游戏失败：{str(e)}',
                        'timestamp': datetime.now().strftime('%H:%M:%S')
                    }
                    emit('new_message', error_msg, room=room['dm'])
                return
        
        # DM的私密查询命令
        if message.startswith('/query '):
            query = message[7:].strip()
            try:
                # 构建包含游戏状态的查询
                context = f"""
                当前游戏状态：
                1. 已选择角色的玩家：
                {get_players_info(room)}
                2. 游戏阶段：{room['state']}
                
                DM的查询：{query}
                """
                
                ai_response, queue_id = get_ai_response(context, room, user_id, is_dm_query=True)
                dm_message = {
                    'user': 'DM助手',
                    'role': 'dm',
                    'content': f"""
**[私密回复]**
{ai_response}
""",
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                    'is_private': True,
                    'queue_id': queue_id
                }
                emit('new_message', dm_message, room=user_id)
            except Exception as e:
                error_message = {
                    'user': '系统',
                    'content': f'AI响应错误：{str(e)}',
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }
                emit('new_message', error_message, room=user_id)
            return
    
    # 处理玩家的角色选择
    if room['state'] == GAME_STATES['CREATING'] and user['role'] == 'player':
        if message.startswith('/choose '):
            handle_character_choice(room, user_id, message[8:])
            return
    
    # 处理玩家的查询命令
    if user['role'] == 'player':
        if message.startswith('/status'):
            # 查询角色状态
            character_info = get_player_character(room, user_id)
            status_message = {
                'user': 'DM助手',
                'role': 'dm',
                'content': f"""
## 你的角色状态
{character_info}
""",
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', status_message, room=user_id)
            return
            
        elif message.startswith('/spell '):
            # 查询法术信息
            spell_name = message[7:].strip()
            spell_info = get_ai_response(
                f"请详细描述D&D中的{spell_name}法术的效果、施法时间、施法材料、持续时间等信息。",
                room,
                user_id
            )
            spell_message = {
                'user': 'DM助手',
                'role': 'dm',
                'content': spell_info,
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', spell_message, room=user_id)
            if room['dm']:
                emit('new_message', spell_message, room=room['dm'])
            return
            
        elif message.startswith('/item '):
            # 查询物品信息
            item_name = message[6:].strip()
            item_info = get_ai_response(
                f"请详细描述D&D中的{item_name}的属性、效果、价值等信息。",
                room,
                user_id
            )
            item_message = {
                'user': 'DM助手',
                'role': 'dm',
                'content': item_info,
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', item_message, room=user_id)
            if room['dm']:
                emit('new_message', item_message, room=room['dm'])
            return
            
        elif message == '/help':
            # 显示帮助信息
            help_message = {
                'user': 'DM助手',
                'role': 'dm',
                'content': """
## 可用命令
- `/status` - 查看你的角色状态
- `/spell <法术名称>` - 查询法术信息
- `/item <物品名称>` - 查询物品信息
- `/help` - 显示此帮助信息

你也可以：
1. 直接描述你的行动
2. 与其他角色互动
3. 询问场景细节
4. 使用角色技能
""",
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', help_message, room=user_id)
            return
    
    # 常规消息处理
    message_data = {
        'user': user['username'],
        'role': user['role'],
        'content': message,
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'sender_id': user_id
    }

    if user['role'] == 'player':
        emit('new_message', message_data, room=user_id)
        if room['dm']:
            emit('new_message', message_data, room=room['dm'])
    else:
        emit('new_message', message_data, room=room_id)
    
    # AI响应处理
    if user['role'] == 'player' and room['state'] == GAME_STATES['PLAYING']:
        try:
            # 获取所有玩家的当前状态
            players_state = get_players_info(room)
            
            # 构建上下文
            context = f"""
            当前场景中的所有角色：
            {players_state}
            
            当前玩家角色：
            {get_player_character(room, user_id)}
            
            玩家行动：
            {message}
            
            请根据玩家的行动生成回应，要求：
            1. 生动描述行动的结果
            2. 考虑其他角色可能的反应
            3. 创造机会促进角色互动
            4. 为其他玩家提供互动的机会
            5. 根据需要推进剧情发展
            """
            
            ai_response, queue_id = get_ai_response(context, room, user_id)
            
            # 发送给当前玩家
            player_message = {
                'user': 'DM助手',
                'role': 'dm',
                'content': ai_response,
                'timestamp': datetime.now().strftime('%H:%M:%S'),
                'sender_id': user_id,
                'queue_id': queue_id
            }
            emit('new_message', player_message, room=user_id)
            
            # 发送给其他玩家和DM
            for pid in room['players']:
                if pid != user_id:
                    emit('new_message', player_message, room=pid)
            if room['dm']:
                emit('new_message', player_message, room=room['dm'])
                
        except Exception as e:
            error_message = {
                'user': '系统',
                'content': f'AI响应错误：{str(e)}',
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', error_message, room=user_id)

def generate_character_options(room):
    """为房间中的每个玩家生成角色选项"""
    try:
        if room['state'] != GAME_STATES['CREATING']:
            raise Exception("游戏状态错误，无法生成角色选项")
            
        if not room['players']:
            raise Exception("房间中没有玩家")
            
        total_players = len(room['players'])
        needed_options = total_players * 3
        
        # 发送详细的开始提示
        start_msg = {
            'user': 'System',
            'content': f"""正在生成游戏内容...

**进度：**
1. ⏳ 正在生成世界背景...
2. 📝 等待生成角色选项 ({total_players} 名玩家，共需 {needed_options} 个角色)
""",
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', start_msg, room=room['dm'])
        
        print(f"开始为 {total_players} 名玩家生成角色选项...")
        
        # 生成世界背景时更新状态
        background_status = {
            'user': 'System',
            'content': f"""正在生成游戏内容...

**进度：**
1. ✨ 世界背景生成完成！
2. ⏳ 正在生成角色选项 ({total_players} 名玩家，共需 {needed_options} 个角色)
""",
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', background_status, room=room['dm'])
        
        # 为每个玩家生成角色时更新状态
        for i, player_id in enumerate(room['players'], 1):
            status_msg = {
                'user': 'System',
                'content': f"""正在生成游戏内容...

**进度：**
1. ✨ 世界背景生成完成！
2. ⏳ 正在生成第 {i}/{total_players} 位玩家的角色选项...
""",
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', status_msg, room=room['dm'])
        
        # 首先生成世界背景
        background_prompt = """请为一个DND游戏创建一个引人入胜的世界背景。包括：
1. 世界的当前状态
2. 主要的冲突或威胁
3. 重要的地理位置
4. 主要的势力
5. 冒险的契机

请用生动的语言描述，让玩家感受到这个世界的魅力。
"""
        background, _ = get_ai_response(background_prompt, room, room['dm'])
        
        # 发送世界背景给所有玩家和DM
        background_message = {
            'user': 'DM助手',
            'content': f"""# 欢迎来到这个奇幻世界！

## 世界背景
{background}

*正在生成角色选项...*""",
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        
        for player_id in room['players']:
            emit('new_message', background_message, room=player_id)
            
        # 发送给DM的世界背景信息
        dm_background_message = {
            'user': 'DM助手',
            'content': f"""# 游戏世界设定

## 世界背景
{background}

*正在为玩家生成角色选项...*

DM提示：
- 使用 /query 命令可以获取更多世界背景细节
- 你可以询问特定地点、势力或NPC的详细信息
- 可以请求剧情发展建议和遭遇设计""",
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', dm_background_message, room=room['dm'])
        
        def validate_character(character, existing_characters):
            """验证角色是否完整且不重复"""
            # 基本字段检查
            required_fields = [
                "名字：", "种族：", "职业：", "属性值：",
                "性格：", "背景故事：", "技能专长：", "装备：", "动机："
            ]
            if not all(field in character for field in required_fields):
                return False, "缺少必要字段"
                
            # 解析当前角色信息
            try:
                name = character.split("名字：")[1].split("\n")[0].strip()
                race = character.split("种族：")[1].split("\n")[0].strip()
                class_info = character.split("职业：")[1].split("\n")[0].strip()
                personality = character.split("性格：")[1].split("\n")[0].strip()
                background = character.split("背景故事：")[1].split("\n")[0].strip()
                
                # 检查与现有角色的相似度
                for existing in existing_characters:
                    e_name = existing.split("名字：")[1].split("\n")[0].strip()
                    e_race = existing.split("种族：")[1].split("\n")[0].strip()
                    e_class = existing.split("职业：")[1].split("\n")[0].strip()
                    
                    # 检查名字相似度
                    if len(set(name.lower()) & set(e_name.lower())) > len(name) * 0.5:
                        return False, "名字过于相似"
                    
                    # 检查种族和职业组合
                    if race == e_race and class_info == e_class:
                        return False, "种族和职业组合重复"
                    
                    # 检查背景故事相似度
                    if len(set(background.split()) & set(existing.split("背景故事：")[1].split("\n")[0].split())) > 10:
                        return False, "背景故事过于相似"
                        
                    # 检查性格描述相似度
                    if len(set(personality.split()) & set(existing.split("性格：")[1].split("\n")[0].split())) > 5:
                        return False, "性格描述过于相似"
                
                return True, ""
                
            except Exception as e:
                return False, f"解析错误：{str(e)}"
        
        def generate_characters(num_chars):
            """生成指定数量的完整且不重复的角色"""
            all_characters = []
            max_attempts = 10  # 最大尝试次数
            attempts = 0
            
            # 记录已使用的组合
            used_combinations = set()
            
            while len(all_characters) < num_chars and attempts < max_attempts:
                try:
                    # 更新状态消息
                    status_msg = {
                        'user': 'System',
                        'content': f"""正在生成角色选项...

**进度：**
- 已生成: {len(all_characters)}/{num_chars} 个角色
- 尝试次数: {attempts + 1}/{max_attempts}""",
                        'timestamp': datetime.now().strftime('%H:%M:%S')
                    }
                    emit('new_message', status_msg, room=room['dm'])
                    
                    # 计算这一批次需要生成多少个角色
                    remaining = num_chars - len(all_characters)
                    batch_size = min(3, remaining)
                    
                    # 更新已使用的种族和职业
                    used_races = [char.split("种族：")[1].split("\n")[0].strip() 
                                 for char in all_characters]
                    used_classes = [char.split("职业：")[1].split("\n")[0].strip() 
                                  for char in all_characters]
                    
                    # 构建提示，包含已使用的组合
                    prompt = f"""基于以下世界背景：
{background}

请生成 {batch_size} 个完全不同的角色。要求：
1. 每个角色必须独特，禁止任何相似性
2. 名字必须富有特色且完全不同
3. 性格和背景故事必须完全不同
4. 团队角色定位要互补
5. 已使用的种族：{', '.join(used_races)}
6. 已使用的职业：{', '.join(used_classes)}
7. 如果可能，优先使用未使用的种族和职业
8. 如果必须重复种族，性格和背景必须完全不同

{CHARACTER_TEMPLATE}"""

                    # 设置超时时间
                    response, _ = get_ai_response(prompt, room, room['dm'], timeout=30)
                    characters = [char.strip() for char in response.split('---分隔线---') if char.strip()]
                    
                    # 验证每个角色
                    for char in characters:
                        is_valid, reason = validate_character(char, all_characters)
                        if is_valid:
                            # 提取种族和职业组合
                            race = char.split("种族：")[1].split("\n")[0].strip()
                            class_info = char.split("职业：")[1].split("\n")[0].strip()
                            combination = f"{race}-{class_info}"
                            
                            # 如果组合未使用过，添加角色
                            if combination not in used_combinations or len(used_combinations) >= num_chars:
                                all_characters.append(char)
                                used_combinations.add(combination)
                                if len(all_characters) >= num_chars:
                                    break
                        else:
                            print(f"角色验证失败：{reason}")
                    
                    attempts += 1
                    
                except Exception as e:
                    print(f"生成角色时出错：{str(e)}")
                    attempts += 1
                    continue
            
            # 如果没有生成足够的角色
            if len(all_characters) < num_chars:
                error_msg = {
                    'user': 'System',
                    'content': f"""角色生成未完成！
- 已生成: {len(all_characters)}/{num_chars} 个角色
- 请DM重新开始游戏""",
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }
                emit('new_message', error_msg, room=room['id'])
                raise Exception("无法生成足够的不重复角色")
            
            return all_characters[:num_chars]
        
        # 生成所有需要的角色
        all_characters_list = generate_characters(needed_options)
        
        # 随机打乱角色列表
        random.shuffle(all_characters_list)
        
        # 为每个玩家分配3个不同的角色
        for i, player_id in enumerate(room['players']):
            start_idx = i * 3
            player_options = '\n\n---分隔线---\n\n'.join(all_characters_list[start_idx:start_idx + 3])
            room['character_options'][player_id] = player_options
            
            # 发送给玩家
            options_message = {
                'user': 'DM助手',
                'content': f"""## 你的可选角色

{player_options}

---

**选择角色说明：**
- 使用 `/choose 1` 选择第一个角色
- 使用 `/choose 2` 选择第二个角色
- 使用 `/choose 3` 选择第三个角色

*选择后将无法更改，请仔细考虑！*""",
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', options_message, room=player_id)
            
            # 发送给DM
            dm_message = {
                'user': 'DM助手',
                'content': f"""## 玩家 {users[player_id]['username']} 的角色选项

{player_options}""",
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', dm_message, room=room['dm'])
            
        print("角色选项生成完成！")
        
    except Exception as e:
        error_message = {
            'user': 'System',
            'content': f'生成角色选项时出错：{str(e)}',
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', error_message, room=room['id'])
        raise e

def handle_character_choice(room, user_id, choice):
    """处理玩家的角色选择"""
    try:
        choice_num = int(choice.strip())
        if 1 <= choice_num <= 3:
            room['selected_characters'][user_id] = choice_num
            
            # 通知玩家选择成功
            message = {
                'user': 'System',
                'content': f'你已选择角色 {choice_num}',
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', message, room=user_id)
            
            # 通知DM
            if room['dm']:
                dm_message = {
                    'user': 'System',
                    'content': f'玩家 {users[user_id]["username"]} 选择了角色 {choice_num}',
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }
                emit('new_message', dm_message, room=room['dm'])
            
            # 检查是否所有玩家都已选择角色
            if len(room['selected_characters']) == len(room['players']):
                room['state'] = GAME_STATES['SCENE']
                start_game_scene(room)
    except ValueError:
        error_message = {
            'user': 'System',
            'content': '请使用正确的格式选择角色：/choose <1-3>',
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', error_message, room=user_id)

def start_game_scene(room):
    """开始游戏场景"""
    try:
        # 获取所有玩家的角色信息
        players_info = get_players_info(room)
        
        # 发送开始加载提示
        loading_message = {
            'user': 'System',
            'content': '正在生成游戏场景...',
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', loading_message, room=room['id'])
        
        # 生成一个简短的开场场景
        scene = get_ai_response(
            f"""简短生成一个开场场景，让所有角色自然相遇。要求：
            1. 场景描述不超过200字
            2. 重点描述角色相遇的契机
            3. 为每个角色预留互动的机会
            
            玩家角色信息：
            {players_info}
            """,
            room,
            None
        )
        
        # 并行处理每个玩家的个性化场景
        def process_player_scene(player_id):
            player = users[player_id]
            character_num = room['selected_characters'][player_id]
            character_options = room['character_options'][player_id].split('\n\n')
            character_info = character_options[character_num - 1]
            
            personal_scene = get_ai_response(
                f"""基于以下信息，简短生成该角色的视角描述：
                
                场景背景：
                {scene}
                
                当前角色信息：
                {character_info}
                
                要求：
                1. 描述不超过150字
                2. 提供2-3个简短的行动建议
                3. 突出角色的个性特点
                """,
                room,
                player_id
            )
            
            # 发送给玩家
            message = {
                'user': 'DM助手',
                'role': 'dm',
                'content': f"""
## 当前场景
{personal_scene}

**你可以：**
- 选择一个建议的行动
- 描述自己的行动
- 与其他角色互动
- 询问更多细节
""",
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            emit('new_message', message, room=player_id)
        
        # 使用线程池并行处理
        with ThreadPoolExecutor(max_workers=4) as executor:
            executor.map(process_player_scene, room['players'])
        
        # 发送给DM
        dm_message = {
            'user': 'DM助手',
            'role': 'dm',
            'content': f"""
## 场景总览
{scene}

## 玩家视角已发送
使用 /query 获取更多信息
""",
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', dm_message, room=room['dm'])
        
        # 更新房间状态
        room['state'] = GAME_STATES['PLAYING']
        
    except Exception as e:
        error_message = {
            'user': 'System',
            'content': f'场景生成错误：{str(e)}',
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', error_message, room=room['id'])

def get_players_info(room):
    """获取房间内所有玩家的角色信息"""
    info = []
    for player_id in room['players']:
        player = users[player_id]
        character_num = room['selected_characters'].get(player_id)
        if character_num:
            character_options = room['character_options'].get(player_id, '').split('\n\n')
            if len(character_options) >= character_num:
                character_info = character_options[character_num - 1]
            else:
                character_info = "未知角色信息"
        else:
            character_info = "尚未选择角色"
        
        info.append(f"""
玩家：{player['username']}
{character_info}
""")
    return "\n".join(info)

def get_player_character(room, player_id):
    """获取指定玩家的角色信息"""
    character_num = room['selected_characters'].get(player_id)
    if character_num:
        character_options = room['character_options'].get(player_id, '').split('\n\n')
        if len(character_options) >= character_num:
            return character_options[character_num - 1]
    return "未知角色信息"

def get_ai_response(message, room, user_id, is_dm_query=False, timeout=None):
    """获取 AI 响应，支持超时设置和重试机制"""
    # 创建进度队列
    queue_id = f"{room['id']}_{user_id}_{int(time.time())}"
    progress_queues[queue_id] = queue.Queue()
    
    def update_progress():
        progress = 0
        while progress < 95:
            time.sleep(0.2)
            progress += 5
            progress_queues[queue_id].put(progress)
    
    progress_thread = threading.Thread(target=update_progress)
    progress_thread.start()
    
    for attempt in range(API_CONFIG['max_retries']):
        try:
            if is_dm_query:
                system_prompt = (
                    "你是一位经验丰富的DND地下城主助手。现在正在和DM进行私密对话。\n"
                    "你需要：\n"
                    "1. 回答DM关于游戏状态的询问\n"
                    "2. 提供怪物数据和地图建议\n"
                    "3. 协助设计剧情发展\n"
                    "4. 平衡游戏难度\n"
                    "5. 提供规则建议\n\n"
                    "请提供详细的信息，包括具体数据和建议。\n"
                    "这是私密对话，只有DM能看到。"
                )
            else:
                system_prompt = SYSTEM_PROMPT
            
            try:
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message}
                    ],
                    temperature=0.7,
                    max_tokens=2000,
                    stream=False
                )
                
                # 获取响应内容
                content = response.choices[0].message.content
                
                progress_queues[queue_id].put('DONE')
                del progress_queues[queue_id]
                return content, queue_id
                
            except Exception as e:
                raise Exception(f"API调用失败：{str(e)}")
            
        except Exception as e:
            if attempt == API_CONFIG['max_retries'] - 1:
                raise Exception(f"多次尝试后失败：{str(e)}")
            print(f"请求失败，第 {attempt + 1} 次重试...")
            time.sleep(API_CONFIG['retry_delay'])
            continue
    
    progress_queues[queue_id].put('DONE')
    del progress_queues[queue_id]
    raise Exception("多次尝试后仍然失败，请检查网络连接")

@app.route('/progress/<queue_id>')
def progress_stream(queue_id):
    """SSE 进度流端点"""
    return Response(
        generate_progress_events(queue_id),
        mimetype='text/event-stream'
    )

def get_local_ip():
    try:
        # 使用 socket 获取本机IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return 'localhost'

# 添加会话恢复相关的函数
def restore_session(user_id):
    """恢复用户会话状态"""
    if user_id not in users:
        return False
        
    user = users[user_id]
    if not user['room']:
        return False
        
    room_id = user['room']
    if room_id not in rooms:
        user['room'] = None
        return False
        
    room = rooms[room_id]
    
    # 重新加入房间
    join_room(room_id)
    join_room(user_id)
    
    # 只给DM发送恢复消息和状态
    if user['role'] == 'dm':
        restore_message = {
            'user': 'System',
            'content': f"""已恢复 {user["username"]} 的连接

{get_game_status(room, user)}""",
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        emit('new_message', restore_message, room=room_id)
    
    return True

def get_game_status(room, user):
    """获取当前游戏状态的描述"""
    user_id = session['user_id']  # 从 session 获取 user_id
    status = f"当前游戏状态：{room['state']}\n\n"
    
    if room['state'] == GAME_STATES['WAITING']:
        if user['role'] == 'dm':
            status += "等待开始游戏..."
        else:
            status += "等待DM开始游戏..."
    
    elif room['state'] == GAME_STATES['CREATING']:
        if user['role'] == 'player':
            if user_id in room['selected_characters']:  # 使用 user_id 而不是 user['id']
                status += f"""您已选择角色，正在等待其他玩家...

当前进度：已有 {len(room['selected_characters'])}/{len(room['players'])} 名玩家选择角色"""
            else:
                status += "请选择您的角色！"
        else:  # DM
            selected = len(room['selected_characters'])
            total = len(room['players'])
            status += f"角色选择阶段：已有 {selected}/{total} 名玩家选择角色"
    
    elif room['state'] == GAME_STATES['PLAYING']:
        if user['role'] == 'player':
            status += f"""游戏进行中！

您的角色：
{get_player_character(room, user_id)}"""  # 使用 user_id 而不是 user['id']
        else:  # DM
            status += f"""游戏进行中！

当前玩家状态：
{get_players_info(room)}"""
    
    return status

# 修改 socketio 连接处理
@socketio.on('connect')
def handle_connect():
    """处理客户端连接"""
    if 'user_id' in session:
        user_id = session['user_id']
        restore_session(user_id)

@socketio.on('disconnect')
def handle_disconnect():
    """处理客户端断开连接"""
    try:
        if 'user_id' in session:
            user_id = session['user_id']
            if user_id in users:
                user = users[user_id]
                if user['room'] and user['room'] in rooms:
                    room = rooms[user['room']]
                    # 只在 DM 断开连接时通知所有玩家
                    if user['role'] == 'dm':
                        disconnect_message = {
                            'user': 'System',
                            'content': 'DM断开连接，请等待重新连接...',
                            'timestamp': datetime.now().strftime('%H:%M:%S')
                        }
                        emit('new_message', disconnect_message, room=room['id'], broadcast=True)
    except Exception as e:
        print(f"断开连接处理错误：{str(e)}")

if __name__ == '__main__':
    host = get_local_ip()
    port = 5000  # 改用5000端口，通常这个端口不会被占用
    print(f"\n=== DND游戏服务器启动 ===")
    print(f"局域网访问地址: http://{host}:{port}")
    print(f"本地访问地址: http://localhost:{port}")
    print("="*30)
    print("提示：确保其他设备与此电脑连接到同一个WiFi网络")
    print(f"如果无法访问，请检查防火墙设置是否允许端口{port}")
    print("="*30)
    
    try:
        socketio.run(
            app, 
            debug=True, 
            host='0.0.0.0',  # 允许外部访问
            port=port,
            allow_unsafe_werkzeug=True  # 允许在开发模式下运行
        )
    except Exception as e:
        print(f"\n启动服务器时出错：{str(e)}")
        print("\n可能的解决方案：")
        print("1. 尝试使用管理员权限运行")
        print("2. 检查端口是否被占用")
        print("3. 检查防火墙设置")
        print("4. 尝试使用其他端口（修改 port 变量）")
        input("\n按回车键退出...") 