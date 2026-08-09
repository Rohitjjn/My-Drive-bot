import time
import math
import psutil
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def humanbytes(size):
    if not size: return "0B"
    power = 2**10
    n = 0
    dic_powerN = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + "" + dic_powerN[n] + 'B'

def time_formatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
          ((str(hours) + "h, ") if hours else "") + \
          ((str(minutes) + "m, ") if minutes else "") + \
          ((str(seconds) + "s") if seconds else "")
    return tmp[:-2] if tmp.endswith(", ") else tmp

def get_sysinfo():
    """Returns formatted system info string using psutil."""
    cpu_usage = psutil.cpu_percent(interval=1)

    mem = psutil.virtual_memory()
    ram_total = humanbytes(mem.total)
    ram_used = humanbytes(mem.used)
    ram_percent = mem.percent

    disk = psutil.disk_usage('/')
    disk_total = humanbytes(disk.total)
    disk_used = humanbytes(disk.used)
    disk_percent = disk.percent

    info_text = (
        "🖥 **System Information:**\n\n"
        f"⚙️ **CPU Usage:** `{cpu_usage}%`\n"
        f"🧠 **RAM:** `{ram_used}` / `{ram_total}` (`{ram_percent}%`)\n"
        f"💾 **Disk:** `{disk_used}` / `{disk_total}` (`{disk_percent}%`)\n"
    )
    return info_text

def create_progress_text(action, current, total, start_time, file_name, cancel_task_id=None):
    now = time.time()
    diff = now - start_time

    percentage = current * 100 / total if total else 0
    speed = current / diff if diff > 0 else 0
    time_to_completion = round((total - current) / speed) * 1000 if speed > 0 else 0

    # Visual Bar [■■■□□□]
    filled_length = int(10 * current // total) if total else 0
    bar = '■' * filled_length + '□' * (10 - filled_length)

    text = f"📂 **File Name:** `{file_name}`\n\n"
    text += f"🚀 **Action:** `{action}`\n"
    text += f"[{bar}] {round(percentage, 2)}%\n\n"
    text += f"💾 **Size:** {humanbytes(current)} / {humanbytes(total)}\n"
    text += f"⚡ **Speed:** {humanbytes(speed)}/sec\n"
    text += f"⏳ **EST Time:** {time_formatter(time_to_completion)}"

    return text
