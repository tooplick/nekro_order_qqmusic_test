import pickle
from pathlib import Path
import aiofiles
from nekro_agent.services.plugin.packages import dynamic_import_pkg

qqmusic_api = dynamic_import_pkg("qqmusic-api-python", "qqmusic_api")
from nekro_agent.api.plugin import NekroPlugin, SandboxMethodType, ConfigBase
from nekro_agent.api.schemas import AgentCtx
from qqmusic_api import search, song
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.login import Credential
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import MessageSegment, ActionFailed
from typing import Any, Literal
from pydantic import Field

plugin = NekroPlugin(
    name="QQ音乐点歌test",
    module_name="order_qqmusic_test",
    description="给予AI助手通过QQ音乐搜索并发送音乐消息的能力",
    version="2.0.5dev",
    author="GeQian",
    url="https://github.com/tooplick/nekro_order_qqmusic_test",
)

@plugin.mount_config()
class QQMusicPluginConfig(ConfigBase):
    """QQ音乐插件配置项"""

    cover_size: Literal["0", "150", "300", "500", "800"] = Field(
        default="500",
        title="专辑封面尺寸",
        description="选择专辑封面尺寸,0表示不发送封面",
        json_schema_extra={
            "description": "支持150x150、300x300、500x500、800x800四种尺寸"
        }
    )
    
    preferred_quality: Literal["FLAC", "MP3_320", "MP3_128"] = Field(
        default="MP3_320",
        title="优先音质",
        description="选择歌曲播放的优先音质，如果无法获取将自动降级",
        json_schema_extra={
            "description": "FLAC为无损音质，MP3_320为高品质，MP3_128为标准品质"
        }
    )

config: QQMusicPluginConfig = plugin.get_config(QQMusicPluginConfig)

async def load_credential() -> Credential | None:
    """加载本地凭证，使用插件持久化目录"""
    try:
        plugin_dir = plugin.get_plugin_path()
        credential_file = plugin_dir / "qqmusic_cred.pkl"

        if not credential_file.exists():
            print("QQ音乐凭证文件不存在")
            return None

        # 使用异步文件读取
        async with aiofiles.open(credential_file, "rb") as f:
            credential_content = await f.read()
        
        # 反序列化凭证
        cred: Credential = pickle.loads(credential_content)
        print("QQ音乐凭证加载成功")
        return cred
    except Exception as e:
        print(f"加载QQ音乐凭证失败: {e}")
        return None

def get_quality_priority(preferred_quality: str) -> list[SongFileType]:
    """根据优先音质返回音质优先级列表"""
    quality_map = {
        "FLAC": [SongFileType.FLAC, SongFileType.MP3_320, SongFileType.MP3_128],
        "MP3_320": [SongFileType.MP3_320, SongFileType.MP3_128],
        "MP3_128": [SongFileType.MP3_128]
    }
    return quality_map.get(preferred_quality, [SongFileType.MP3_320, SongFileType.MP3_128])

async def get_song_url(song_info: dict, credential: Credential, preferred_quality: str) -> str:
    """根据优先音质获取歌曲下载链接，失败时自动降级"""
    mid = song_info['mid']
    
    # 获取音质优先级列表
    quality_priority = get_quality_priority(preferred_quality)
    
    quality_names = {
        SongFileType.FLAC: "FLAC无损",
        SongFileType.MP3_320: "MP3高品质",
        SongFileType.MP3_128: "MP3标准"
    }
    
    last_exception = None
    
    # 按照优先级尝试不同音质
    for file_type in quality_priority:
        try:
            urls = await get_song_urls([mid], file_type=file_type, credential=credential)
            url = urls[mid] if isinstance(urls[mid], str) else urls[mid][0]
            if url:
                quality_name = quality_names.get(file_type, str(file_type))
                print(f"使用{quality_name}音质")
                return url
        except Exception as e:
            last_exception = e
            quality_name = quality_names.get(file_type, str(file_type))
            print(f"{quality_name}音质获取失败: {e}")
            continue
    
    # 所有音质都失败
    raise ValueError(f"无法获取歌曲下载链接，所有音质尝试均失败。最后错误: {last_exception}")

def get_cover(mid: str, size: int = 300) -> str | None:
    """获取专辑封面链接"""
    if size == 0:
        return None  # 尺寸为0时不发送封面
    if size not in [150, 300, 500, 800]:
        raise ValueError("封面尺寸必须是150、300、500或800")
    return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{mid}.jpg"

def parse_chat_key(chat_key: str) -> tuple[str, int]:
    """解析chat_key，返回聊天类型和目标ID"""
    if "_" not in chat_key:
        raise ValueError(f"无效的 chat_key: {chat_key}")
    
    adapter_id, old_chat_key = chat_key.split("-", 1)
    chat_type, target_id = old_chat_key.split("_", 1)
    
    if not target_id.isdigit() or chat_type not in ("private", "group"):
        raise ValueError(f"chat_key 格式错误: {chat_key}")
    
    return chat_type, int(target_id)

async def send_message(bot, chat_type: str, target_id: int, message) -> bool:
    """发送消息的通用函数"""
    try:
        if chat_type == "private":
            await bot.call_api("send_private_msg", user_id=target_id, message=message)
        else:
            await bot.call_api("send_group_msg", group_id=target_id, message=message)
        return True
    except ActionFailed as e:
        print(f"发送消息失败: {e}")
        return False

@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="send_music_test",
    description="搜索 QQ 音乐并发送歌曲信息、专辑封面和语音消息"
)
async def send_music_test(
        _ctx: AgentCtx,
        chat_key: str,
        keyword: str
) -> str:
    """
    搜索 QQ 音乐歌曲并发送给用户（文字+封面+语音）

    Args:
        _ctx (AgentCtx): 插件调用上下文
        chat_key (str): 会话标识，例如"onebot_v11-private_12345678" 或 "onebot_v11-group_12345678"
        keyword (str): 搜索关键词：歌曲名 歌手名

    Returns:
        str: 发送结果提示信息，例如 "歌曲《xxx》已发送"
    """
    try:
        bot = get_bot()

        # 加载凭证
        credential = await load_credential()
        if not credential:
            return "QQ音乐凭证不存在，无法播放歌曲"

        # 搜索歌曲
        result = await search.search_by_type(keyword=keyword, num=1)
        if not result:
            return "未找到相关歌曲"
        
        first_song = result[0]
        mid = first_song["mid"]
        singer = first_song["singer"][0]["name"]
        title = first_song["title"]

        # 使用模块级配置获取封面尺寸，并转换为整数
        cover_size = int(config.cover_size)
        
        # 获取专辑封面
        cover_url = get_cover(first_song["album"]["mid"], size=cover_size)
        
        # 获取播放链接（使用配置的优先音质）
        music_url = await get_song_url(first_song, credential, config.preferred_quality)

        # 解析 chat_key
        chat_type, target_id = parse_chat_key(chat_key)

        # 发送文字消息
        message_text = f"{title}-{singer}"
        if not await send_message(bot, chat_type, target_id, message_text):
            return "发送文字消息失败"

        # 发送专辑封面
        if cover_url:
            cover_msg = MessageSegment.image(cover_url)
            if not await send_message(bot, chat_type, target_id, cover_msg):
                return "发送专辑封面失败"

        # 发送语音消息
        voice_msg = MessageSegment.record(file=music_url)
        if not await send_message(bot, chat_type, target_id, voice_msg):
            return "发送语音消息失败"

        
        # 发送音乐卡片
        music_card = MessageSegment.music(
            type="custom",
            url=f"https://y.qq.com/n/ryqq/songDetail/{mid}",
            audio=music_url,
            title=title,
            image=cover_url
        )  
        
        if not await send_message(bot, chat_type, target_id, music_card):
            return "发送音乐卡片失败"

    

        return f"歌曲《{title}》已发送"

    except Exception as e:
        print(f"发送音乐消息时发生错误: {e}")
        return f"发送音乐消息失败: {e}"

@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件资源"""
    print("QQ音乐插件资源已清理")