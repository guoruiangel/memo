# Memo

Pablo 和郭锐的私聊工具。

## 功能
- 对话消息持久化
- 任务看板（打分+归档+删除）
- 引用回复
- 自动唤醒Pablo

## 启动
```bash
cd memo
pip install flask waitress
python server.py
```

默认端口 5003，访问 http://localhost:5003/

## 数据
- pablo_chat.db — 消息和任务数据
- 模板在 templates/pablo_chat.html
