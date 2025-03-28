import sqlite3
from flask import Flask, render_template, jsonify, request
from datetime import datetime
import json

app = Flask(__name__)

def get_db_connection():
    conn = sqlite3.connect('interactions.db')
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_interactions')
def get_interactions():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 获取所有记录
    cursor.execute('SELECT id, model, conversation, timestamp FROM interactions')
    rows = cursor.fetchall()
    
    # 处理数据
    data = []
    for row in rows:
        try:
            # 解析conversation JSON字符串
            conversation = json.loads(row['conversation'])
            # 提取对话内容的简短预览
            preview = ''
            if 'conversations' in conversation:
                for msg in conversation['conversations']:
                    preview += f"{msg['from']}: {msg['value'][:50]}...\n"
            
            data.append({
                'id': row['id'],
                'model': row['model'],
                'conversation': preview,
                'full_conversation': conversation,
                'timestamp': row['timestamp']
            })
        except json.JSONDecodeError:
            continue
    
    conn.close()
    return jsonify({'data': data})

@app.route('/delete', methods=['POST'])
def delete_interaction():
    data = request.get_json()
    interaction_id = data.get('id')
    
    if not interaction_id:
        return jsonify({'error': '未提供ID'}), 400
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM interactions WHERE id = ?', (interaction_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/confirm', methods=['POST'])
def confirm_interaction():
    data = request.get_json()
    interaction_id = data.get('id')
    
    if not interaction_id:
        return jsonify({'error': '未提供ID'}), 400
    
    conn = get_db_connection()
    try:
        # 获取要确认的记录
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM interactions WHERE id = ?', (interaction_id,))
        record = cursor.fetchone()
        
        if record:
            # 创建confirmed_interactions表（如果不存在）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS confirmed_interactions (
                    id TEXT PRIMARY KEY,
                    model TEXT,
                    conversation TEXT,
                    original_timestamp DATETIME,
                    confirmed_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 插入到confirmed_interactions表
            cursor.execute('''
                INSERT INTO confirmed_interactions (id, model, conversation, original_timestamp)
                VALUES (?, ?, ?, ?)
            ''', (record['id'], record['model'], record['conversation'], record['timestamp']))
            
            # 从原表删除
            cursor.execute('DELETE FROM interactions WHERE id = ?', (interaction_id,))
            
            conn.commit()
            return jsonify({'success': True})
        else:
            return jsonify({'error': '记录不存在'}), 404
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    app.run(debug=True)