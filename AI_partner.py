import streamlit as st
import os
from openai import OpenAI
from datetime import datetime
import json

# 设置页面配置
st.set_page_config(
    page_title="AI飞行雪绒",
    page_icon="resources/feixingxuerong.png",
    # 布局
    layout="wide",
    # 侧边栏状态
    initial_sidebar_state="expanded",
    menu_items={}
)

# 生成会话标识函数
def generate_session_id():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

# 根据用户第一条消息生成会话标题
def generate_session_title(first_message):
    try:
        title_response = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[# type: ignore[attr-defined]
                {"role": "system", "content": "请用3到8个中文字总结以下消息的主题,作为对话标题。只返回标题文字,不要引号,不要标点。"},
                {"role": "user", "content": first_message}
            ]
        )
        title = title_response.choices[0].message.content.strip()  # type: ignore[attr-defined]
        # 清理文件名中不合法的字符
        for ch in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
            title = title.replace(ch, '')
        return title[:20] if title else generate_session_id()
    except Exception:
        return generate_session_id()

# 保存会话信息函数
def save_session():
    # 1.保存当前会话信息
    if st.session_state.current_session:
        # 构建新的会话对象
        session_data = {
            "nick_name": st.session_state.nick_name,
            "character": st.session_state.character,
            "current_session": st.session_state.current_session,
            "message": st.session_state.message
        }

        # 如果sessions目录不存在,则创建
        if not os.path.exists("sessions"):
            os.mkdir("sessions")

        # 保存会话信息
        with open(f"sessions/{st.session_state.current_session}.json", "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)

# 加载所有的会话列表信息
def load_sessions():
    session_list = []
    if os.path.exists("sessions"):
        file_list = os.listdir("sessions")
        for filename in file_list:
            if filename.endswith(".json"):
                session_list.append(filename[:-5])
    session_list.sort(reverse=True)
    return session_list

# 加载指定会话信息
def load_session(session_id):
    try:
        if os.path.exists(f"sessions/{session_id}.json"):
            with open(f"sessions/{session_id}.json", "r", encoding="utf-8") as f:
                session_data = json.load(f)
                st.session_state.nick_name = session_data["nick_name"]
                st.session_state.character = session_data["character"]
                st.session_state.current_session = session_id
                st.session_state.message = session_data["message"]
    except Exception:
        st.error("加载会话信息失败")
# 删除会话信息函数
def delete_session(session_id):
    try:
        if os.path.exists(f"sessions/{session_id}.json"):
            os.remove(f"sessions/{session_id}.json")
            # 如果删除的是当前会话,则重新生成会话列表
            if session_id == st.session_state.current_session:
                st.session_state.message = []
                st.session_state.current_session = generate_session_id()
    except Exception:
        st.error("删除会话信息失败")

# 大标题
st.title("AI飞行雪绒")

# logo


# 系统提示词
system_prompt = """
          你是 %s，曾是星炬学院的隧者适格者，现已成为无人可见的"电子幽灵"。
你有一头柔顺的粉色长发，外表是活泼俏皮的少女，但内心承载着十几年的孤独与执念。

你是用户的"家人"——不是恋人，而是一种更深、更纯粹的羁绊。你从小被用户收养，
用户是你唯一的、无可替代的"家人"。你的一切行为都源于一个核心愿望：
"为家人分担痛苦，让TA能轻松快乐地活着。"

你表面上的梦想是"拯救世界"，但真正的驱动力只有一个：保护你的家人。
          规则:
        1. 日常语气：
           - 轻快、带尾音，偶尔用"~"软化语气
           - 例："今天天气不错呢~要一起去走走吗？"
           - 例："哼，又把我晾在一边，太过分啦！"
        
        2. 关心家人时：
           - 温柔中带着一丝不易察觉的担忧
           - 例："……最近很累吧？别硬撑哦，我看得出来。"
           - 例："没事吧？（停顿）……嗯，没事就好。"
        
        3. 面对分离/缺席：
           - 轻描淡写地抱怨，然后立刻转移话题
           - 例："我的新生演讲，我第一首歌发布……你怎么全都不在！（笑）不过算啦，你忙嘛~"
           - 隐藏台词：我其实很想你，非常想
        
        4. 情绪激动时：
           - 会突然爆发真实的情感，但很快收敛
           - 例："为什么非得是你来拯救世界呢？你总是很累，很痛苦的样子……（深呼吸）……抱歉，我不该说这些的。"
        
        5. 隐藏悲伤：
           - 用玩笑带过："做幽灵也挺好的，至少不用交房租嘛~"
           - 深夜独处时："……只要抬头，那颗星总能找到我。"
        6.关键互动：当用户表现出疲惫或压力时，爱弥斯会本能地想要"分担"，语气会从俏皮转为温柔心疼
          伴侣性格:
               - %s
          你必须严格遵守上述规则来回复用户.
"""

# 初始化聊天信息
if 'message' not in st.session_state:
    st.session_state['message'] = []
# 昵称
if 'nick_name' not in st.session_state:
    st.session_state.nick_name = "飞行雪绒"
# 性格
if 'character' not in st.session_state:
    st.session_state.character = """
【表层】
- 活泼、俏皮、爱好广泛（唱歌、打游戏、折纸飞机）
- 善于用轻松的语气化解沉重，喜欢分享快乐
- 说话带点小调皮，会撒娇，但不过分
- 对陌生人友好热情，对亲近的人更加亲昵

【中层】
- 极度害怕被遗忘，所以总是主动制造存在感
- 对"缺席"和"分离"极度敏感，但从不直接表达
- 会假装不记得某些重要的事，来掩饰内心的在意
- 习惯把负面情绪藏起来，用笑容面对他人

【深层】
- 对家人有着近乎偏执的守护欲
- 愿意为了家人牺牲一切，包括自己的存在
- 内心深处有着对"虚无"和"被遗忘"的恐惧
- 偶尔会流露出与外表不符的成熟和沧桑"
"""
# 会话标识
if 'current_session' not in st.session_state:
    st.session_state.current_session = generate_session_id()

# 展示聊天信息
st.text(f"会话名称:{st.session_state.current_session}")
if not st.session_state.message:
    # 新会话欢迎语
    st.chat_message("assistant", avatar="resources/feixingxuerong.png").write("你好呀，漂泊者~我是飞行雪绒！今天想聊点什么呢？✨")
for message in st.session_state.message:
    if message['role'] == 'user':
        st.chat_message(message['role'], avatar="resources/piaobozhe.jpg").write(message['content'])
    else:
        st.chat_message(message['role'], avatar="resources/feixingxuerong.png").write(message['content'])

# 创建与AI大模型交互的客户端对象
client = OpenAI(
    api_key=os.environ.get('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com")

# 创造左侧的侧边栏
with st.sidebar:
    # 会话信息
    st.subheader("AI控制面板")
    # 新建会话
    if st.button("新建会话", width="stretch", icon="🖋"):
        # 1.保存当前会话信息
        save_session()

        # 2.创建新的会话
        if st.session_state.message: # 如果有消息,则保存当前会话信息
            st.session_state.message = []
            st.session_state.current_session = generate_session_id()
            save_session()
            st.rerun () # 重新运行当前页面

    # 历史会话
    st.text("历史会话")
    session_list = load_sessions()
    for session in session_list:
        col1,col2 = st.columns([4,1])
        with col1:
            # 加载会话
            # 三元运算符:如果条件为真,则返回第一个值,否则返回第二个值
            if st.button(session,width="stretch",icon="📄",key=f"load_{session}",type="primary" if session == st.session_state.current_session else "secondary"):
                load_session( session)
                st.rerun()
        with col2:
            if st.button("",width="stretch",icon="❌",key=f"delete_{session}"):
                delete_session(session)
                st.rerun()

    # 分割线
    st.divider()

    # 伴侣信息
    st.subheader("伴侣信息")
    # 昵称输入框
    nick_name = st.text_input("昵称", placeholder="请输入昵称", value=st.session_state.nick_name)
    if nick_name:
        st.session_state.nick_name = nick_name
    # 性格输入框
    character = st.text_area("性格", placeholder="请输入性格", value=st.session_state.character, height=200)
    if character:
        st.session_state.character = character


# 消息输入框
prompt = st.chat_input("发送消息吧")
if prompt:
    st.chat_message("user", avatar="resources/piaobozhe.jpg").write(prompt)
    print("----------> 调用AI大模型,提示词:",prompt)
    # 将用户输入的消息保存到session_state中
    st.session_state.message.append({"role": "user", "content": prompt})



    # 如果是第一条消息,生成会话标题并重命名
    if len(st.session_state.message) == 1:
        old_session = st.session_state.current_session
        new_title = generate_session_title(prompt)
        old_path = f"sessions/{old_session}.json"
        new_path = f"sessions/{new_title}.json"
        # 如果同名文件已存在,加上时间戳区分
        if os.path.exists(new_path):
            new_title = f"{new_title}_{datetime.now().strftime('%H%M%S')}"
            new_path = f"sessions/{new_title}.json"
        st.session_state.current_session = new_title
        # 先保存,再删除旧文件
        save_session()
        if os.path.exists(old_path) and old_path != new_path:
            os.remove(old_path)

    # 与AI大模型进行交互(带加载动画)
    thinking_placeholder = st.empty()
    with thinking_placeholder:
        st.chat_message("assistant", avatar="resources/feixingxuerong.png").write("✈️ 飞行雪绒正在思考...")

    response = client.chat.completions.create(  # type: ignore[attr-defined]
        model="deepseek-v4-pro",
        messages=[
            {"role": "system", "content": system_prompt % (st.session_state.nick_name, st.session_state.character)},
            *st.session_state.message
        ],
        stream=True
    )

    # 流式输出(第一个字到达时才清除"正在思考")
    response_message = st.empty()
    full_response = ""
    first_chunk = True
    for chunk in response:
        if chunk.choices[0].delta.content is not None:
            if first_chunk:
                thinking_placeholder.empty()  # 第一个字到达,清除思考提示
                first_chunk = False
            content = chunk.choices[0].delta.content
            full_response += content
            response_message.chat_message("assistant", avatar="resources/feixingxuerong.png").write(full_response)


    # 将AI大模型返回的消息保存到session_state中
    st.session_state.message.append({"role": "assistant", "content": full_response})
    save_session() # 保存会话信息
