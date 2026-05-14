"""
飞书周报汇报系统 - Flask后端
"""
import json
import re
import os
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID, FEISHU_WRITE_APP_TOKEN, FEISHU_WRITE_TABLE_ID, MEMBERS

app = Flask(__name__)
CORS(app)

# 测试模式：不调用飞书API，直接返回模拟数据
TEST_MODE = False

# 数据存储模式
# True = 周报数据存本地JSON文件（推荐，飞书写入权限经常受限）
# False = 周报数据写入飞书BITable（需要飞书应用有写入权限）
FALLBACK_MODE = False
LOCAL_DATA_DIR = os.path.join(os.path.dirname(__file__), "local_data")

# 飞书API基础地址
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

# 请求超时设置（秒）
REQUEST_TIMEOUT = 10

# 确保本地数据目录存在
os.makedirs(LOCAL_DATA_DIR, exist_ok=True)

def get_local_file_path():
    """获取当前周的本地数据文件路径"""
    week = get_week_number()
    return os.path.join(LOCAL_DATA_DIR, f"weekly_report_{week}.json")

def load_local_data():
    """从本地JSON文件加载数据"""
    file_path = get_local_file_path()
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"records": []}

def save_local_data(data):
    """保存数据到本地JSON文件"""
    file_path = get_local_file_path()
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def check_feishu_connection():
    """检查飞书API是否可达"""
    global FALLBACK_MODE
    try:
        url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
        data = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
        response = requests.post(url, json=data, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            FALLBACK_MODE = False
            return True
    except Exception as e:
        print(f"[WARN] 飞书API不可达，切换到Fallback模式: {e}")
        FALLBACK_MODE = False
        return False
    FALLBACK_MODE = False
    return False

# 获取飞书访问令牌
def get_tenant_access_token():
    url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
    result = response.json()
    return result.get("tenant_access_token")


def get_headers():
    token = get_tenant_access_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def get_week_number():
    """获取当前周数，格式 YYYY-WW"""
    now = datetime.now()
    week = now.isocalendar()[1]
    return f"{now.year}-{week:02d}"


def get_current_week_sheet_name():
    """获取当前周的工作表名称"""
    return f"周报记录-{get_week_number()}"


def ensure_report_sheet_exists(headers):
    """确保本周周报记录工作表存在，不存在则创建"""
    # 获取所有工作表
    url = f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables"
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    result = response.json()
    
    if result.get("code") != 0:
        print(f"获取表格失败: {result}")
        # 使用预设的周报表格 ID
        return FEISHU_REPORT_TABLE_ID
        
    tables = result.get("data", {}).get("items", [])

    week_sheet_name = get_current_week_sheet_name()
    sheet_id = None

    # 检查工作表是否已存在
    for table in tables:
        if table.get("name") == week_sheet_name:
            sheet_id = table.get("table_id")
            break

    if not sheet_id:
        # 创建新工作表
        create_url = f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables"
        create_data = {"table": {"name": week_sheet_name}}
        response = requests.post(create_url, headers=headers, json=create_data, timeout=REQUEST_TIMEOUT)
        result = response.json()
        print(f"创建表格响应: {result}")
        
        if result.get("code") != 0:
            print(f"创建表格失败，使用预设表格ID: {FEISHU_REPORT_TABLE_ID}")
            # 创建失败时使用预设的周报表格 ID
            return FEISHU_REPORT_TABLE_ID
            
        sheet_id = result.get("data", {}).get("table_id")

        if sheet_id:
            # 添加字段
            fields_url = f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{sheet_id}/fields"
            fields = [
                {"field_name": "姓名", "type": 1},
                {"field_name": "提交时间", "type": 5},
                {"field_name": "任务完成情况", "type": 1},
                {"field_name": "政策与建议", "type": 1},
                {"field_name": "风险反馈", "type": 1},
                {"field_name": "下周重点", "type": 1},
                {"field_name": "总分", "type": 2},
            ]
            for field in fields:
                requests.post(fields_url, headers=headers, json=field, timeout=REQUEST_TIMEOUT)

    return sheet_id


def calculate_score(data):
    """计算周报得分
    
    新的评分逻辑基于 task_completions 数组格式：
    - 任务维度：按 content 非空数量计算（满分40分）
    - 思考维度：thought_1 + thought_2（满分30分）
    - 反馈维度：feedback_1 + feedback_2（满分30分）
    """
    # 任务得分（满分40分）- 基于 task_completions 数组
    task_completions = data.get("task_completions", [])
    total_tasks = len(task_completions)
    filled_tasks = sum(1 for task in task_completions if task.get("content", "").strip())
    
    if filled_tasks == total_tasks and total_tasks > 0:
        # 全部填满
        task_score = 40
    elif filled_tasks >= 3:
        # 有内容 ≥ 3项，认真填写
        task_score = 32
    elif filled_tasks >= 1:
        # 有内容 ≥ 1项
        task_score = 24
    elif filled_tasks == 0:
        # 全空，0分
        task_score = 0
    else:
        task_score = 0

    # 思考得分（满分30分）- 板块2有2个字段
    thought_items = [
        data.get("thought_1", "").strip(),
        data.get("thought_2", "").strip(),
    ]
    thought_filled = sum(1 for item in thought_items if item)
    if thought_filled >= 2:
        thought_score = 30
    elif thought_filled == 1:
        thought_score = 15
    else:
        thought_score = 0

    # 反馈得分（满分30分）- 板块3有2个字段
    feedback_items = [
        data.get("feedback_1", "").strip(),
        data.get("feedback_2", "").strip(),
    ]
    feedback_filled = sum(1 for item in feedback_items if item)
    if feedback_filled >= 2:
        feedback_score = 30
    elif feedback_filled == 1:
        feedback_score = 15
    else:
        feedback_score = 0

    total = task_score + thought_score + feedback_score

    return {
        "task_score": task_score,
        "thought_score": thought_score,
        "feedback_score": feedback_score,
        "total": total
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    """获取飞书表格中的任务列表，按负责人分组"""
    # 测试模式：返回空任务列表
    if TEST_MODE:
        return jsonify({"tasks": {name: [] for name in MEMBERS}, "mode": "test"})
    try:
        headers = get_headers()
        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
        params = {"page_size": 500}

        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        result = response.json()

        if result.get("code") != 0:
            return jsonify({"error": result.get("msg", "Failed to fetch tasks")}), 500

        records = result.get("data", {}).get("items", [])

        # 按负责人分组
        tasks_by_person = {name: [] for name in MEMBERS}

        for record in records:
            fields = record.get("fields", {})
            title = fields.get("任务标题", "")
            person = fields.get("负责人", "")

            # 匹配负责人名称
            # 特殊处理：微积分、联合会工作号 → 归到吴剑峰
            person_str = str(person)
            matched_person = None
            for name in MEMBERS:
                if name in person_str:
                    matched_person = name
                    break
            # 如果没有直接匹配到成员名字，检查是否是吴剑峰的别名
            if not matched_person:
                aliases = ["微积分", "联合会工作号"]
                for alias in aliases:
                    if alias in person_str:
                        matched_person = "吴剑峰"
                        break

            if matched_person and title:
                tasks_by_person[matched_person].append({
                    "record_id": record.get("record_id"),
                    "title": str(title)
                })

        return jsonify({"tasks": tasks_by_person})

    except Exception as e:
        return jsonify({"error": str(e), "tasks": {name: [] for name in MEMBERS}}), 500


@app.route("/api/submit", methods=["POST"])
def submit_report():
    """提交周报，计算得分并保存"""
    try:
        data = request.json
        name = data.get("name", "")

        if not name or name not in MEMBERS:
            return jsonify({"error": "Invalid name"}), 400

        # 计算得分
        scores = calculate_score(data)

        # 测试模式：直接返回成功
        if TEST_MODE:
            print(f"[TEST MODE] Submit received for: {name}, score: {scores['total']}")
            return jsonify({
                "success": True,
                "scores": [
                    {
                        "name": name,
                        "task_score": scores["task_score"],
                        "thought_score": scores["thought_score"],
                        "feedback_score": scores["feedback_score"],
                        "total": scores["total"]
                    }
                ],
                "personal_score": {
                    "name": name,
                    **scores
                }
            })

        # Fallback模式：保存到本地JSON文件
        if FALLBACK_MODE:
            print(f"[FALLBACK MODE] Saving to local file for: {name}")
            local_data = load_local_data()

            # 检查是否已提交过，覆盖旧记录
            local_data["records"] = [r for r in local_data["records"] if r.get("name") != name]

            # 添加新记录（使用新的 task_completions 数组格式）
            local_data["records"].append({
                "name": name,
                "submit_time": datetime.now().isoformat(),
                "task_completions": data.get("task_completions", []),
                "thought_1": data.get("thought_1", ""),
                "thought_2": data.get("thought_2", ""),
                "feedback_1": data.get("feedback_1", ""),
                "feedback_2": data.get("feedback_2", ""),
                "task_score": scores["task_score"],
                "thought_score": scores["thought_score"],
                "feedback_score": scores["feedback_score"],
                "total": scores["total"]
            })

            save_local_data(local_data)

            # 获取本地所有得分
            all_scores = get_local_scores()

            return jsonify({
                "success": True,
                "scores": all_scores,
                "personal_score": {
                    "name": name,
                    **scores
                },
                "mode": "fallback"
            })

        # 写入飞书新表格（应用自建，有完整写入权限）
        headers = get_headers()
        timestamp = int(datetime.now().timestamp() * 1000)  # 毫秒时间戳

        record_data = {
            "fields": {
                "姓名": name,
                "提交时间": timestamp,
                "任务完成情况": json.dumps(data.get("task_completions", []), ensure_ascii=False),
                "思考字段1": data.get("thought_1", ""),
                "思考字段2": data.get("thought_2", ""),
                "反馈字段1": data.get("feedback_1", ""),
                "反馈字段2": data.get("feedback_2", ""),
                "总分": scores["total"]
            }
        }

        # 写入新表格
        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_WRITE_APP_TOKEN}/tables/{FEISHU_WRITE_TABLE_ID}/records"
        response = requests.post(url, headers=headers, json=record_data, timeout=REQUEST_TIMEOUT)
        result = response.json()

        if result.get("code") != 0:
            print(f"[ERROR] 飞书API返回错误: {result}")
            return jsonify({"error": f"飞书API错误: {result.get('msg', 'Unknown error')}"}), 500

        # 获取所有已提交得分
        scores_response = get_all_scores()

        return jsonify({
            "success": True,
            "scores": scores_response.get("scores", []),
            "personal_score": {
                "name": name,
                **scores
            }
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_local_scores():
    """从本地JSON文件获取所有得分"""
    local_data = load_local_data()
    scores = []
    submitted_names = set()

    for record in local_data.get("records", []):
        # 兼容处理：支持新旧两种格式
        task_completions = record.get("task_completions", [])
        if not task_completions and record.get("tasks"):
            # 旧格式：tasks 是按人分组的字典
            tasks_data = record.get("tasks", {})
            task_completions = []
            for person_tasks in tasks_data.values():
                for task in person_tasks:
                    task_completions.append({
                        "record_id": task.get("record_id", ""),
                        "content": "已完成" if task.get("completed") else ""
                    })

        # 计算任务得分
        total_tasks = len(task_completions)
        filled_tasks = sum(1 for task in task_completions if task.get("content", "").strip())

        if filled_tasks == total_tasks and total_tasks > 0:
            task_score = 40
        elif filled_tasks >= 3:
            task_score = 32
        elif filled_tasks >= 1:
            task_score = 24
        elif filled_tasks == 0:
            task_score = 12
        else:
            task_score = 0

        # 思考得分
        thought_1 = record.get("thought_1", "").strip()
        thought_2 = record.get("thought_2", "").strip()
        thought_filled = sum(1 for item in [thought_1, thought_2] if item)
        thought_score = 30 if thought_filled >= 2 else (15 if thought_filled == 1 else 0)

        # 反馈得分
        feedback_1 = record.get("feedback_1", "").strip()
        feedback_2 = record.get("feedback_2", "").strip()
        feedback_filled = sum(1 for item in [feedback_1, feedback_2] if item)
        feedback_score = 30 if feedback_filled >= 2 else (15 if feedback_filled == 1 else 0)

        scores.append({
            "name": record.get("name", ""),
            "task_score": task_score,
            "thought_score": thought_score,
            "feedback_score": feedback_score,
            "total": record.get("total", 0)
        })
        submitted_names.add(record.get("name"))

    # 添加未提交成员
    for name in MEMBERS:
        if name not in submitted_names:
            scores.append({
                "name": name,
                "task_score": 0,
                "thought_score": 0,
                "feedback_score": 0,
                "total": 0,
                "pending": True
            })

    # 按分数排序
    scores.sort(key=lambda x: (-x["total"], x["name"]))
    return scores


@app.route("/api/scores", methods=["GET"])
def get_scores_route():
    """获取所有已提交的得分"""
    return jsonify(get_all_scores())


def get_all_scores():
    """获取所有已提交得分（返回dict，供内部调用和路由共用）"""
    # Fallback模式
    if FALLBACK_MODE or TEST_MODE:
        return {"scores": get_local_scores(), "mode": "fallback"}

    try:
        headers = get_headers()

        # 从新表格读取所有周报记录
        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_WRITE_APP_TOKEN}/tables/{FEISHU_WRITE_TABLE_ID}/records"
        params = {"page_size": 500}
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        records = response.json().get("data", {}).get("items", [])

        scores = []
        submitted_names = set()

        for record in records:
            fields = record.get("fields", {})
            name = fields.get("姓名", "")
            total = int(fields.get("总分", 0) or 0)

            # 解析任务完成情况（支持新旧两种格式）
            tasks_json = fields.get("任务完成情况", "[]")
            try:
                tasks_data = json.loads(tasks_json)
            except:
                tasks_data = []

            # 兼容处理：如果解析出来是字典（旧格式），转换为数组
            if isinstance(tasks_data, dict):
                task_completions = []
                for person_tasks in tasks_data.values():
                    for task in person_tasks:
                        task_completions.append({
                            "record_id": task.get("record_id", ""),
                            "content": "已完成" if task.get("completed") else ""
                        })
                tasks_data = task_completions

            # 计算任务得分
            total_tasks = len(tasks_data)
            filled_tasks = sum(1 for task in tasks_data if task.get("content", "").strip())

            if filled_tasks == total_tasks and total_tasks > 0:
                task_score = 40
            elif filled_tasks >= 3:
                task_score = 32
            elif filled_tasks >= 1:
                task_score = 24
            elif filled_tasks == 0:
                task_score = 12
            else:
                task_score = 0

            # 思考得分 - 板块2有2个字段
            thought_1 = fields.get("思考字段1", "").strip()
            thought_2 = fields.get("思考字段2", "").strip()
            thought_filled = sum(1 for item in [thought_1, thought_2] if item)
            thought_score = 30 if thought_filled >= 2 else (15 if thought_filled == 1 else 0)

            # 反馈得分 - 板块3有2个字段
            feedback_1 = fields.get("反馈字段1", "").strip()
            feedback_2 = fields.get("反馈字段2", "").strip()
            feedback_filled = sum(1 for item in [feedback_1, feedback_2] if item)
            feedback_score = 30 if feedback_filled >= 2 else (15 if feedback_filled == 1 else 0)

            scores.append({
                "name": name,
                "task_score": task_score,
                "thought_score": thought_score,
                "feedback_score": feedback_score,
                "total": total
            })
            submitted_names.add(name)

        # 添加未提交成员
        for name in MEMBERS:
            if name not in submitted_names:
                scores.append({
                    "name": name,
                    "task_score": 0,
                    "thought_score": 0,
                    "feedback_score": 0,
                    "total": 0,
                    "pending": True
                })

        # 按分数排序
        scores.sort(key=lambda x: (-x["total"], x["name"]))

        return {"scores": scores}

    except Exception as e:
        print(f"[ERROR] 飞书API调用失败，自动切换到Fallback模式: {e}")
        # API失败时自动切换到本地存储
        return {"scores": get_local_scores(), "mode": "fallback", "error": str(e)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
