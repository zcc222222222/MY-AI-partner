"""v2 知识库的路径与常量。

目录约定:代码在 knowledge/ 包里,**运行时产物落在包的上一级 data/** 且被 git 忽略。
镜像下来的第三方数据不进版本库 —— 它随时可能消失,也不该由这个仓库分发。

⚠️ 这里刻意**不写死 "v2" 这一层**。这不是为了某个已定的重构计划(目前没有),
   而是因为"写死目录层级"本身是个容易后悔的做法:一旦哪天要调整布局,
   被写死的地方会散落在各个文件里,而且不会报错 —— 只会静默把数据写到别的地方去。
   现在只需要一处 ``git mv``,代码一行都不用动。
"""
import os


def _find_repo_root(start: str) -> str:
    """向上找含 .git 的目录。

    用于定位**仓库**(判断运行时产物有没有被 git 忽略),不用于定位数据 ——
    数据用"包的上一级",因为那是搬家中唯一保持不变的相对关系。
    """
    current = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start)
        current = parent


HERE = os.path.dirname(os.path.abspath(__file__))
# 包的上一级。现在是 v2/;若这个包将来被移到别处(例如 backend/knowledge/),
# 数据会自动跟着变成 backend/data/,不需要改代码 —— 已实测过。
PACKAGE_ROOT = os.path.dirname(HERE)
REPO_ROOT = _find_repo_root(HERE)

# 运行时产物。可用 KB_DATA_DIR 覆盖(测试、CI、或把数据放别的盘时用)。
DATA_DIR = os.environ.get("KB_DATA_DIR") or os.path.join(PACKAGE_ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
DB_PATH = os.path.join(DATA_DIR, "knowledge.db")
MAPS_DIR = os.path.join(HERE, "maps")

# 数据源。版本号在 URL 里,这是整个增量更新的基础(见 nanoka.py)。
SOURCE_BASE = "https://static.nanoka.cc/ww"
SITE_ROOT = "https://ww.nanoka.cc/"
DEFAULT_LOCALE = "zh"

# 版本根目录下的索引文件:语言无关,内部内联了 zh/en/ja/ko 名称。
ROOT_FILES = ("character.json", "echo.json", "weapon.json", "sonata.json", "monster.json")
# 语言子目录下的文件。
LOCALE_FILES = ("item.json", "hyperlink.json")
# 每个角色一个独立文件,id 从 character.json 的键推出来。
CHARACTER_DETAIL = "character/{id}.json"

# 采集礼节:单线程、每次请求后至少等待默认间隔,请求层另设最小启动间隔。
# 是否存在限流不能凭以前的观察断言;429/503 见 nanoka.py 的退避处理。
DEFAULT_DELAY_SECONDS = 1.5
MIN_DELAY_SECONDS = 1.0
USER_AGENT = "MY-AIPATRNER-knowledge-mirror/0.1 (personal project; contact: repo owner)"

ELEMENT_MAP_FILE = os.path.join(MAPS_DIR, "element.json")
WEAPON_MAP_FILE = os.path.join(MAPS_DIR, "weapon.json")


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR):
        os.makedirs(d, exist_ok=True)
