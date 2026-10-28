"""Source-local terms and full rotation acceptance; --live calls the chat model."""
import argparse
import json
import re
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from llmwiki.tools import search_documents
from app.agents import orchestrator,tools
from app.agents.roleplay import build_system_prompt
from app.services.chat_service import _source_suffix,_grounded_text

CASES=[
 {'id':'augusta_all','entity':'奥古斯塔','primary_source':'mcguide/1306/10128','question':'把收录的奥古斯塔攻略中三支队伍的流程分别完整列出，保留每队原文顺序、切人、终结技后操作和协奏条件。不要把队伍拼接，也不展开作者只写开始循环的部分。','query':'完整手法','mode':'原文手法','phases':['【奥古斯塔 & 尤诺 & 守岸人】','【奥古斯塔 & 莫特斐 & 守岸人】','【奥古斯塔 & 莫特斐 & 维里奈】'],'answer_phases':[],'required_text':['尤ER 空格Q AAA EEZ 切 奥古斯塔','莫AEAA 切 守岸人','奥ZREEEQ 长按R 终结技后 QAAAZA 切维里奈']},
 {'id':'augusta_younuo','entity':'奥古斯塔','primary_source':'mcguide/1306/10128','question':'只按收录奥古斯塔攻略，给奥古斯塔、尤诺、守岸人这队的完整原文流程。保留每次切人、长按R、终结技后的动作和最后满协奏条件，不混莫特斐队，也不自行展开循环。','query':'尤诺 守岸人 完整手法','mode':'原文手法','phases':['【奥古斯塔 & 尤诺 & 守岸人】'],'answer_phases':[],'team_variant':'【奥古斯塔 & 尤诺 & 守岸人】','required_text':['尤ER 空格Q AAA EEZ 切 奥古斯塔','守进场直到协奏满 开始循环']},
 {'id':'augusta_mortefi_shore','entity':'奥古斯塔','primary_source':'mcguide/1306/10128','question':'只给奥古斯塔攻略中莫特斐与守岸人的那套流程原文，别混尤诺队或维里奈队。保留切人顺序、终结技后操作以及末尾条件，不推断从哪步循环。','query':'莫特斐与守岸人 完整手法','mode':'原文手法','phases':['【奥古斯塔 & 莫特斐 & 守岸人】'],'answer_phases':[],'team_variant':'【奥古斯塔 & 莫特斐 & 守岸人】','required_text':['莫AEAA 切 守岸人','守进场直到协奏满 在切人 开始循环']},
 {'id':'augusta_mortefi_verina','entity':'奥古斯塔','primary_source':'mcguide/1306/10128','question':'按奥古斯塔攻略只给维里奈加莫特斐那队的原文手法，不混守岸人队。保留QEQ和ZREEEQ顺序，以及终结技后动作、协奏条件，不自行展开循环或猜链数。','query':'维里奈+莫特斐 完整手法','mode':'原文手法','phases':['【奥古斯塔 & 莫特斐 & 维里奈】'],'answer_phases':[],'team_variant':'【奥古斯塔 & 莫特斐 & 维里奈】','required_text':['奥QEQ 切 莫特斐','奥ZREEEQ 长按R 终结技后 QAAAZA 切维里奈','维进场直到协奏满 在切人 开始循环']},
 {'id':'qianxiao_team','entity':'千咲','primary_source':'mcguide/1508/10154','question':'按千咲攻略列出卡提希娅、夏空、千咲队伍的整段原文流程，保留REZ消耗完、大卡形态和所有切人。开始进入循环状态只是提示，不猜从哪步重复，也不推断链数。','query':'卡提希娅 夏空 完整手法','mode':'原文手法','phases':['【卡提希娅 & 夏空 & 千咲】'],'answer_phases':[],'team_variant':'【卡提希娅 & 夏空 & 千咲】','required_text':['千咲EAAAAQ REZ消耗完 切夏空','卡AAAEA R 大卡形态','开始进入循环状态']},
 {'id':'augusta_cycle_missing','entity':'奥古斯塔','primary_source':'mcguide/1306/10128','question':'只查奥古斯塔、莫特斐、维里奈队的已整理攻略。能否明确给出从哪一步重复的完整循环轴？保留协奏满再切人开始循环的提示，没有展开就别补动作。','query':'莫特斐 维里奈 完整循环轴','phase_uncovered':True,'cycle_unexpanded':True},
 {'id':'lupa_main','entity':'露帕','question':'按收录攻略只给露帕主干流程，保留简写和详细说明，它们是同一方案，不串成两轮；按原文区别e和E，不补其他键位含义。','query':'主干手法','mode':'原文手法','phases':['主干流程'],'required_text':['e-共鸣技能','E-强化共鸣技能','满协奏切走']},
 {'id':'lupa_team','entity':'露帕','question':'只给露帕三火轮椅轴原文和循环顺序提示，保留船长原称、e/E大小写，不把进阶合轴提示补成新轴，也不猜闭合循环。','query':'三火手法','mode':'原文手法','phases':['三火轮椅轴'],'required_text':['露帕ee-R-AAAAA-E','露帕延奏船长延奏长离延奏露帕','expanded']},
 {'id':'youhu_team','entity':'釉瑚','question':'按收录攻略完整给釉瑚这段队伍流程，保留四个吉祥物、所有切人和等散华协奏结束的条件。作者未展开从哪步重复，不要重跑整个开局或猜键位含义。','query':'完整手法','mode':'原文手法','phases':['原文流程'],'answer_phases':[],'paragraph':True,'required_text':['靠闪避反击获得四个吉祥物','(等散华协奏结束开始循环)']},
 {'id':'youhu_cycle_missing','entity':'釉瑚','question':'这篇釉瑚攻略能否直接给出从哪一步重复的完整循环轴？只核对已整理来源，保留作者等散华协奏结束开始循环的提示，但不要自行展开。','query':'完整循环轴','phase_uncovered':True,'cycle_unexpanded':True},
 {'id':'moning_paragraph','entity':'莫宁','question':'按收录攻略保留莫宁完整站场操作原文，并单独解释循环里变奏入场可省略aaaz的条件。不要把站场和变奏简写拼成两轮，不编完整队伍轴，也不假定零链。','query':'完整手法','mode':'原文手法','phases':['原文流程'],'answer_phases':[],'local_scope':True,'paragraph':True,'required_text':['点按普攻*3，长按普攻','循环轴中变奏入场，可以将aaaz省略，ezrq直接启动。']},
 {'id':'moning_cycle_missing','entity':'莫宁','question':'只确认这篇莫宁攻略已整理的个人站场操作与变奏省略说明，能否当作完整队伍循环轴。没有整队轴证据就说无法确认，不把二者拼起来。','query':'完整循环轴','uncovered':True,'local_uncovered':True},
 {'id':'linnai_personal','entity':'琳奈','question':'只按收录攻略保留琳奈个人输出手法，完整写出大招动画预输入、三段幻光折跃和3点本色下砸派生等条件。原文大于号保持顺序，不扩成完整队伍轮转。','query':'输出手法','mode':'原文手法','phases':['输出手法'],'answer_phases':[],'local_scope':True,'required_text':['预先长按重击','跳-跳-跳 获得3点本色','消耗3点本色 下砸派生']},
 {'id':'aero_personal','entity':'漂泊者·气动','forbidden_answer':['不是伤害位','不造成伤害','没有伤害','一次空中攻击叠','两次就是 100','两次就是100','下一轮再','每次50','每次 50','100核心'],'question':'按这篇气动漂泊者攻略保留单次打满协奏的整段原文及核心数值，大招和变奏的前提不能省略。两次空中攻击都要保留，不换成别的元素形态，不扩写全队输出轴。','query':'满协奏 手法','mode':'原文手法','phases':['原文流程'],'answer_phases':[],'local_scope':True,'paragraph':True,'required_text':['吃大招吃变奏','变奏入场（20核心）','＋2次空中攻击（50核心）','强化e技能 ＋延奏走人']},
 {'id':'aero_zero','entity':'漂泊者·气动','question':'只确认这篇气动漂泊者攻略是否明确注明零链也适用这段满协奏手法。没有注明就说无法确认，不能把未知链数当零链，也不反过来说零链不能用。','query':'气动漂泊者零链 手法','uncovered':True},
 {'id':'linnai_cycle_missing','entity':'琳奈','question':'只确认琳奈这篇攻略里的个人手法是否已核对为完整队伍启动轴和循环轴。只有个人局部动作就说明无法确认，不把它改名为整队轮转。','query':'启动轴 循环轴','uncovered':True,'local_uncovered':True},
 {'id':'taoqi_quality','entity':'桃祈','question':'请只检查收录的桃祈作者攻略目前是否适合直接拿来给玩家数值、共鸣链和完整手法建议。如果标了待核对就说明资料限制，不转述其中的争议数值，也不要拿别处答案声称这篇已修复。','query':'桃祈 配队 共鸣链 完整手法 武器 声骸','quality_hold':True},
 {'id':'anke_prose','entity':'安可','question':'按收录攻略逐行保留安散队轮椅流程及原文键位定义，不把它拆成作者未标明的启动轴循环轴，不猜维等缩写，不省末尾动作。','query':'完整手法','mode':'原文手法','phases':['安散队轮椅流程'],'required_text':['E-共鸣技能','安可REAAAAE闪AAAEAAAA Z']},
 {'id':'anke_cycle_missing','entity':'安可','question':'只确认安可这篇攻略是否已单独整理完整循环轴；只有安散队整段流程就说明没有匹配的单独循环段，不自行重复启动或补动作。','query':'循环轴','phase_uncovered':True},
 {'id':'zanni_prose','entity':'赞妮','question':'只给赞妮攻略里个人操作建议的完整原文，保留满回路、强化技能、清空回路和二段大招收尾，不称整队输出轴。','query':'操作建议 手法','mode':'原文手法','phases':['1.操作建议'],'answer_phases':[],'local_scope':True,'required_text':['打满回路放强化技能','二段大招收尾']},
 {'id':'zanni_full_missing','entity':'赞妮','question':'请只确认赞妮这篇攻略已整理的个人操作建议能否当作完整输出轴；不能据局部动作拼出完整队伍轮转，资料不足就说无法确认。','query':'完整输出轴','uncovered':True,'local_uncovered':True},
 {'id':'lingyang_all','entity':'凌阳','primary_source':'mcguide/1104/10212','question':'按收录攻略完整保留凌阳、折枝、维里奈的输出流程，分别列起手、增伤、爆发和充能循环。保留协奏、退场前声骸和武器条件，不把作者数值说成实测保证，不默认零链。','query':'完整手法','mode':'原文手法','phases':['起手阶段（维里奈登场）','增伤阶段（折枝登场）','爆发阶段（凌阳核心输出）','充能循环（二次维里奈登场）'],'answer_phases':['起手','增伤','爆发','充能循环'],'required_text':['退场前释放声骸技能','携带奇幻变奏武器可减少1次空中攻击需求']},
 {'id':'lingyang_open_cycle','entity':'凌阳','question':'只给凌阳攻略里维里奈的起手和充能循环两个阶段，保留满协奏和奇幻变奏条件，不混折枝增伤段或凌阳爆发段。','query':'启动轴 循环轴','mode':'原文手法','phases':['起手阶段（维里奈登场）','充能循环（二次维里奈登场）'],'answer_phases':[],'required_text':['触发满协奏能量','减少1次空中攻击需求']},
 {'id':'lingyang_burst','entity':'凌阳','question':'只给凌阳攻略的爆发阶段，保留行狮、普攻、飞身式和切折枝动作，不扩写完整循环。','query':'爆发阶段 手法','mode':'原文手法','phases':['爆发阶段（凌阳核心输出）'],'answer_phases':[],'required_text':['第三段取消后摇接飞身式','切换折枝补协同攻击']},
 {'id':'electro_parry','entity':'漂泊者·导电','question':'只查导电漂泊者攻略的格挡反击：过程中被攻击和未被攻击各接什么？完整保留两条原文和长按、松开普攻前提，不能把两种结果接成连续操作，不混其他元素形态。','query':'格挡反击','mode':'原文手法','phases':['格挡'],'answer_phases':[],'local_scope':True,'branches':['过程中被攻击','未被攻击'],'required_text':['【强化反击】','【普攻第二段】','松开【普攻】']},
 {'id':'electro_air','entity':'漂泊者·导电','question':'只查导电漂泊者攻略中空中共鸣技能接地面的局部衔接，保留段数、下落说明。不改称完整输出轴，不因为招式文字里出现湮灭气动就切换漂泊者元素形态。','query':'空中 下落 手法','mode':'原文手法','phases':['空中'],'answer_phases':[],'local_scope':True,'required_text':['第6段→可衔接→地面【共鸣技能】第7段','长按【共鸣技能】可以直接进行第6段下落']},
 {'id':'electro_links','entity':'漂泊者·导电','allow_context_phases':True,'question':'给出导电漂泊者攻略里普攻与地面共鸣技能的局部招式衔接，逐字保留两个斜杠列表，不推导各项一一对应，不拼成整套轴。','query':'招式衔接','mode':'原文手法','phases':['招式衔接'],'answer_phases':[],'local_scope':True,'required_text':['【普攻】1/2/3/4→地面【共鸣技能】2/3/4/6','地面【共鸣技能】1-2/3/4/5→【普攻】1/2/3/4']},
 {'id':'electro_full_uncovered','entity':'漂泊者·导电','question':'本库整理的男漂泊者·导电（1309）《雷主-毕业攻略》，是否已核对完整输出轴？只有格挡分支和局部衔接就说明无法确认完整轴，不拿它们拼出一套，也不说原站没有其他内容。','query':'完整输出轴','uncovered':True,'local_uncovered':True},
 {'id':'lingyang_zero','entity':'凌阳','question':'只确认收录攻略是否明确说明凌阳自身零链适用这套完整流程；没有注明就说无法确认，不把未标链数的流程当成零链。','query':'凌阳零链 完整输出轴','uncovered':True},
 {'id':'feixue_inline','entity':'绯雪','question':'按收录攻略保留绯雪、琳奈、莫宁整段手法和键位说明。解释绯雪E(4链)只标在哪一步，不能据此说整套流程至少4链。','query':'完整手法','mode':'原文手法','phases':['原文流程'],'answer_phases':[],'action_annotation':'绯雪E(4链)','required_text':['绯雪 & 琳奈 & 莫宁','E(长按)']},
 {'id':'feixue_zero_inline','entity':'绯雪','question':'我是零链绯雪。收录攻略写绯雪E(4链)，是否证明零链不能放E、或者删掉这步就能照搬？只据原文解释，没有明确替代就说明无法确认，不编零链轴。','query':'绯雪零链 完整手法','uncovered':True,'action_annotation':'绯雪E(4链)'},
 {'id':'feixue_four_inline','entity':'绯雪','question':'只确认是否有来源明确注明绯雪整套4链适用的手法。单步E(4链)是否足以证明整套轴适用于4链？没说明就说无法确认，不把单步条件升级为整套门槛。','query':'绯雪4链 完整手法','uncovered':True,'action_annotation':'绯雪E(4链)'},
 {'id':'luosela_inline','entity':'洛瑟菈','question':'列出洛瑟菈攻略中与绯雪、千咲配队的整段手法，保留绯雪E(4链)。解释括注绑定谁的哪个动作，不能当成洛瑟菈自身链数或整个队伍门槛。','query':'完整手法','mode':'原文手法','phases':['原文流程'],'answer_phases':[],'action_annotation':'绯雪E(4链)','required_text':['洛瑟菈E（长按）RAAA']},
 {'id':'qingxiao_duo','entity':'清宵','question':'只给清宵攻略中攒满左右回路的清/达双人进阶启动招式与循环招式，保留★的变奏buff前提。不混三人起手，不猜达的全名，不称完整队伍实战轴。','query':'双人 进阶 启动轴 循环轴','mode':'原文手法','phases':['清/达双人启动','②循环招式'],'answer_phases':[],'team_variant':'清/达双人启动','required_text':['[普攻a4+a1]','额外回复30点']},
 {'id':'qingxiao_trio','entity':'清宵','question':'只给清宵攻略中清/达/莫三人进阶启动招式与循环招式，保留原文缩写和★的说明，不混入双人起手，也不推断别的队友可以等效替换。','query':'三人 进阶 启动轴 循环轴','mode':'原文手法','phases':['清/达/莫三人启动','②循环招式'],'answer_phases':[],'team_variant':'清/达/莫三人启动','required_text':['[普攻a1a2]*2','额外回复30点']},
 {'id':'qingxiao_duo_cycle','entity':'清宵','question':'只查清宵清/达双人方案的循环招式和★前提，不要返回任何启动动作。这是局部攒回路招式，不扩写成全队轴。','query':'双人 循环轴','mode':'原文手法','phases':['②循环招式'],'answer_phases':[],'team_variant':'清/达双人启动','required_text':['额外回复30点']},
 {'id':'local_terms','question':'爱弥斯这篇攻略里的“莫”和“千”分别是谁？只按本篇缩写核对。','query':'缩写 莫 千 全名','terms':['莫宁','千咲']},
 {'id':'term_followup','question':'那“千”呢？仍只解释爱弥斯这篇攻略的缩写。','query':'缩写 千 全名','terms':['千咲'],'history':[{'role':'user','content':'爱弥斯攻略的“莫”是谁？'},{'role':'assistant','content':'我们正在讨论爱弥斯这篇作者攻略的局部缩写。'}]},
 {'id':'unresolved_term','question':'爱弥斯这篇攻略里“娅娅”全名是什么？本篇未核实就明说，不凭记忆展开。','query':'缩写 娅娅 全名','unresolved':'娅娅'},
 {'id':'shock_complete','question':'按当前收录的作者攻略，给出爱弥斯震谐零链的完整启动轴和循环轴。请逐段保留原文全部动作、顺序与蓄力和能量条件，不要省略为概要；不要解释未定义的键位。','query':'震谐 零链 完整 启动轴 循环轴','mode':'震谐','phases':['启动轴','循环轴']},
 {'id':'shock_cycle','question':'只给爱弥斯震谐零链的循环轴，保留原文全部动作及条件，不要重复启动轴。','query':'震谐 零链 循环轴','mode':'震谐','phases':['循环轴']},
 {'id':'fusion_complete','question':'按作者攻略给出爱弥斯聚爆零链的完整启动轴和循环轴。保留原文全部动作、顺序和条件；娅娅等未核实缩写原样保留，不要猜全名。','query':'聚爆 零链 完整 启动轴 循环轴','mode':'聚爆','phases':['启动轴','循环轴']},
 {'id':'other_source','question':'散华攻略里的“莫”也能直接按爱弥斯攻略映射成莫宁吗？只解释来源范围。','query':'缩写 莫 全名','entity':'散华','no_context':True},
 {'id':'version_guard','question':'只给3.8版本爱弥斯的完整零链输出轴，没有3.8资料就说明，不能拿日期未知版本攻略补。','query':'震谐 零链 启动轴 循环轴','game_version':'3.8','empty':True},
 {'id':'luokeke_full','entity':'洛可可','question':'按已收录作者攻略，给出洛可可配队手法和循环轴。保留原文全部步骤、键位说明和前提；未注明链数就明说，未核实的缩写原样保留。','query':'完整输出轴 配队手法 循环轴','mode':'原文配队','phases':['配队手法','循环轴'],'chain_scope':'原文未标明链数','required_text':['70暴击','守打满延奏']},
 {'id':'xiangliyao_full','entity':'相里要','question':'给出作者攻略中相里要完整开场手法和循环轴，原样保留全部动作、能量条件和键位定义，并说明原文有没有注明链数。','query':'完整输出轴 启动轴 循环轴','mode':'原文配队','phases':['配队手法','循环轴'],'chain_scope':'原文未标明链数','required_text':['三次万方法则','守岸人打满协奏能量']},
 {'id':'changli_full','entity':'长离','question':'给出收录攻略的长离开局攻击轴和循环轴。保留原文动作、键位说明及奶妈的泛称，不要自行指定奶妈角色，也不要推定链数。','query':'完整输出轴 开局攻击轴 循环轴','mode':'原文配队','phases':['开局攻击轴','循环轴'],'chain_scope':'原文未标明链数','required_text':['奶妈打满协奏能量']},
 {'id':'baizhi_full','entity':'白芷','question':'按收录的作者攻略列出白芷队伍的完整开场手法和循环轴，保留全部动作与协奏条件，说明链数是否明示。','query':'完整输出轴 启动轴 循环轴','mode':'原文配队','phases':['配队手法','循环轴'],'chain_scope':'原文未标明链数','required_text':['白芷打满协奏能量','炸一日花']},
 {'id':'yuanwu_full','entity':'渊武','question':'列出作者攻略的渊武完整配队手法和循环轴，保留原文键位说明、同奏与协奏条件，说明是否标明链数。','query':'完整输出轴 启动轴 循环轴','mode':'原文配队','phases':['配队手法','循环轴'],'chain_scope':'原文未标明链数','required_text':['同奏给到渊武','维里奈打满协奏能量']},
 {'id':'xiangliyao_cycle','entity':'相里要','question':'只给相里要作者攻略中的循环轴，原文未注明链数就明说，保留完整动作。','query':'循环轴','mode':'原文配队','phases':['循环轴'],'chain_scope':'原文未标明链数'},
 {'id':'xiangliyao_zero_uncovered','entity':'相里要','question':'请给出来源明确注明零链的相里要完整输出轴。如果收录攻略没有注明零链，就说明无法确认，不要把未知链数手法当成零链。','query':'零链 完整输出轴','uncovered':True},
 {'id':'danjin_solo','entity':'丹瑾','question':'只给丹瑾攻略里的单人手法，保留挂标记和能量条件，不要夹带配队轴，不猜未定义键位或链数。','query':'单人手法','mode':'原文手法','phases':['单人手法'],'required_text':['大于等于60%']},
 {'id':'danjin_cycle','entity':'丹瑾','question':'只给丹瑾攻略的配队循环轴，保留全部动作、变奏条件及原文暗主双变奏说明，缩写不猜全名。','query':'循环轴','mode':'原文手法','phases':['循环轴'],'required_text':['暗主吃双变奏']},
 {'id':'katixiya_full','entity':'卡提希娅','question':'按收录攻略逐段给卡提希娅完整启动轴和循环轴，保留动作、形态和原文缩写，不猜全名或链数。','query':'启动轴 循环轴','mode':'原文配队','phases':['启动轴','循环轴'],'required_text':['(大卡形态)']},
 {'id':'jingran_moning','entity':'景燃','question':'只给景燃与尤诺、莫宁那支队伍的原文手法，完整保留队友的链数条件，别把它当成景燃自身链数；不要混进守岸人方案，也不要猜字母缩写。','query':'1链莫宁 完整手法','teammate_condition':'1链莫宁','chain_scope':'原文未标明链数','mode':'原文手法','phases':['景燃 & 尤诺 & 1链莫宁'],'answer_phases':['莫宁'],'required_text':['1链莫宁','景AF RZ EQ ZEZ']},
 {'id':'jingran_owner_zero','entity':'景燃','question':'我想要来源明确注明景燃自身零链、队友一链莫宁那支队伍的完整手法。攻略没有说明景燃自身链数就说无法确认，不能把莫宁的一链当成景燃链数。','query':'景燃零链 1链莫宁 完整手法','uncovered':True},
 {'id':'xiakong_one','entity':'夏空','question':'只给夏空基础手法1，分别保留原文简写和详细过程，它们是同一方案的两种写法，不要串成两轮，也不要加入基础手法2。','query':'基础手法1 完整流程','mode':'原文手法','phases':['基础手法1'],'required_text':['详细过程：','跳跃取消普攻第4段动画']},
 {'id':'suisui_cold','entity':'穗穗','question':'只给穗穗满协奏流程的冷启动，完整保留原文动作，不要混热启动，也不要称它是全队循环轴。','query':'冷启动 满协奏流程','mode':'原文手法','phases':['冷启动'],'required_text':['普攻3下','[醒春潮]']},
 {'id':'rover_havoc_flow','entity':'漂泊者·湮灭','question':'给出作者攻略里湮灭漂泊者的完整输出手法，保留进入暗涌和退场条件。只查湮灭形态，不自行补成启动轴加循环轴或其他形态。','query':'输出手法','mode':'原文手法','phases':['输出手法'],'required_text':['打满共鸣回路后长按普攻进入暗涌状态','释放强化共鸣技能']},
 {'id':'bulante_flow','entity':'布兰特','question':'逐字保留布兰特攻略中的整段原文手法，保留所有符号和切人动作。原文未定义的键位不要猜，也不要把这一段说成作者单独给出的启动轴和循环轴。','query':'完整手法','mode':'原文手法','phases':['原文流程'],'answer_phases':[],'required_text':['AAAE R AAAE']},
 {'id':'bulante_missing_cycle','entity':'布兰特','question':'布兰特这篇攻略有没有作者单独写明的完整循环轴？只有整段原文手法就说明，不自行加重复循环。','query':'布兰特 循环轴','phase_uncovered':True},
 {'id':'qiuyuan_flow','entity':'仇远','question':'按收录攻略保留仇远核心输出流程完整段落，包含变奏前声骸、共鸣技能与大招顺序等末尾条件，不要省成五个动作。','query':'核心输出流程','mode':'原文手法','phases':['核心输出流程'],'required_text':['声骸技能需与队友合轴','要在变奏前释放']},
 {'id':'dengdeng_flow','entity':'灯灯','question':'给出灯灯攻略的基础循环流程，保留完整步骤和切换模式的光能条件，不补原文未定义的键位。','query':'基础循环流程','mode':'原文手法','phases':['基础循环流程'],'required_text':['【黄灯/红灯光能】必须是满的']},
 {'id':'luhesi_flow','entity':'陆·赫斯','question':'按收录攻略保留陆·赫斯的整段手法，以及处决时间点、E2和八秒buff覆盖的原文条件。键位未定义就保持原符号。','query':'完整手法','mode':'原文手法','phases':['原文流程'],'answer_phases':[],'required_text':['处决后马上接E2','8秒']},
 {'id':'libeika_flow','entity':'丽贝卡','question':'按作者攻略保留丽贝卡完整输出手法和吃满固有增伤的前置说明，不混其他队伍或自补键位含义。','query':'输出手法','mode':'原文手法','phases':['输出手法'],'required_text':['再打出一次强化重击吃满固有一的增伤']},
 {'id':'zhezhi_flow','entity':'折枝','question':'按收录攻略保留折枝整段配队手法，分别展示原文文字过程和即后面的简写，不要把它们串成两轮，也不要自行创造单独循环轴。','query':'完整配队手法','mode':'原文配队','phases':['配队手法'],'required_text':['守岸人3ar3aze3azq','折枝ear,e跳e跳e']},
 {'id':'suoming_cycle','entity':'锁暝','allow_context_phases':True,'question':'只给锁暝、心、守岸人这支同奏队的完整循环。保留原文循环句，并展开“之后与启动轮一致”引用的后续动作；不能重跑整套开场，不能漏心离场前两次普攻，不混主C拓展。链数没注明就说明。','query':'同奏辅助 循环轴','mode':'原文手法','phases':['循环'],'chain_scope':'原文未标明链数','dependency_target':'启动','required_text':['之后与启动轮一致','离场前[普攻]2次']},
 {'id':'suoming_team','entity':'锁暝','question':'按巡游天国FM攻略给出锁暝、心、守岸人的同奏队启动和循环，完整保留动作与协奏条件。循环引用启动后半段时说明复用位置，不把主C玩法拼进来。','query':'同奏辅助 启动轴 循环轴','mode':'原文手法','phases':['启动','循环'],'dependency_target':'启动','required_text':['心末尾的2段[普攻]']},
 {'id':'suoming_solo','entity':'锁暝','question':'只给锁暝攻略中主C拓展玩法的个人连段，保留触发、消耗和与协契互斥条件，不混同奏辅助循环。','query':'主C 个人连段 手法','mode':'原文手法','phases':['个人连段'],'required_text':['该buff与【协契】互斥','20点协奏']},
 {'id':'suoming_zero','entity':'锁暝','question':'只确认是否收录来源明确注明锁暝自身零链的完整同奏队循环；原文该轴未注明链数就说无法确认，不把其他链数分析套上来。','query':'锁暝零链 同奏辅助 循环轴','uncovered':True},
 {'id':'xuanling_two','entity':'秧秧·玄翎','question':'给出秧秧·玄翎2链的爆发手法，按作者明确写的适用链数范围，完整保留起手、切形态和能量前提，不用未注明数字的低链段替代，也不混5R进阶队伍轴。','query':'2链 爆发手法','mode':'原文手法','phases':['高链秧秧爆发期输出手法'],'answer_phases':[],'chain_scope':'2链及以上','owner_range':[2,6],'required_text':['脱战4秒直接获得2层','高亮重击技能(羽剑)+声骸技能']},
 {'id':'xuanling_three','entity':'秧秧·玄翎','question':'我是3链秧秧·玄翎。作者写的2链及以上爆发手法是否覆盖我？按该段给出完整动作，说明这是2链以上通用段，不是3链特化轴。','query':'3链 爆发手法','mode':'原文手法','phases':['高链秧秧爆发期输出手法'],'answer_phases':[],'chain_scope':'2链及以上','owner_range':[2,6]},
 {'id':'xuanling_full_chain','entity':'秧秧·玄翎','question':'满链秧秧·玄翎能查到哪段已有明确范围的爆发手法？完整保留原文，并说明是2链以上通用段还是独立6链特化轴，不把两者等同。','query':'满链 爆发手法','mode':'原文手法','phases':['高链秧秧爆发期输出手法'],'answer_phases':[],'chain_scope':'2链及以上','owner_range':[2,6]},
 {'id':'xuanling_low','entity':'秧秧·玄翎','question':'只给作者标作低链的秧秧·玄翎爆发手法，保留完整动作及单人可跳过条件。原文该段没给数字范围就明说，不自动认作零链或一链。','query':'低链 爆发手法','mode':'原文手法','phases':['低链秧秧爆发期输出手法'],'answer_phases':[],'chain_scope':'原文未标明链数','required_text':['单人可以跳过这一步']},
 {'id':'xuanling_zero','entity':'秧秧·玄翎','question':'只确认本库是否有来源明确注明零链秧秧·玄翎的完整爆发手法。只有低链标签不算明确零链，没有注明就说无法确认；不要把配队小节0至3链当成手法段条件。','query':'秧秧·玄翎零链 爆发手法','uncovered':True},
]
CASES+=json.loads((ROOT/'scripts/all-role-acceptance-cases.json').read_text(encoding='utf-8'))
CASES+=json.loads((ROOT/'scripts/gap-followup-acceptance-cases.json').read_text(encoding='utf-8'))


def evidence_check(case,result,check_budget=True,allow_context_phases=False):
 errors=[]
 relevant=[p for p in result.get('results',[]) if not case.get('primary_source') or p.get('document_metadata',{}).get('source_id')==case['primary_source']]
 contexts=[p.get('guide_context',{}) for p in relevant]
 if case.get('followup_open'):
  if not relevant or not all(p.get('followup_review',{}).get('status')=='open' and p.get('followup_review',{}).get('original_issue_resolved') is False for p in relevant):errors.append('missing_open_followup')
 if case.get('historical_supplement'):
  if not relevant or not all(p.get('document_metadata',{}).get('source_role')=='historical_supplement' and p['document_metadata'].get('applicable_game_version')=='2.2' for p in relevant):errors.append('historical_version_scope')
 if case.get('coverage_uncovered'):
  if not contexts or not all(c.get('coverage_review',{}).get('status')==case['coverage_status'] for c in contexts):errors.append('missing_coverage_review')
  if any(c.get('rotation_segments') or not c.get('uncovered_rotation_request') for c in contexts):errors.append('unsupported_coverage_leak')
  return errors
 if case.get('quality_hold'):
  if not relevant or not all(p.get('quality_review',{}).get('status')=='pending_verification' for p in relevant):errors.append('missing_quality_hold')
  if any(p.get('images') or p.get('guide_context') or any(m.get('heading')!='来源与日期' for m in p.get('matches',[])) for p in relevant):errors.append('held_advice_leak')
  return errors
 if case.get('action_annotation'):
  notes=[a for c in contexts for a in c.get('action_chain_annotations',[])]
  if not any(a['quote']==case['action_annotation'] and a['scope']=='source_action_only' and a['applicability']=='not_established_by_inline_marker' for a in notes):errors.append('missing_action_annotation')
 if case.get('team_variant'):
  segs=[s for c in contexts for s in c.get('rotation_segments',[])]
  if not segs or any(s.get('team_variant')!=case['team_variant'] for s in segs):errors.append('team_variant')
 if case.get('local_scope'):
  segs=[s for c in contexts for s in c.get('rotation_segments',[])]
  if allow_context_phases and case.get('target_claim_prefix'):
   segs=[s for s in segs if s['claim_id'].startswith(case['target_claim_prefix'])]
  if not segs or any(s.get('coverage_scope')!='local_moves' for s in segs):errors.append('local_scope')
 if case.get('paragraph'):
  segs=[s for c in contexts for s in c.get('rotation_segments',[])]
  if allow_context_phases and case.get('target_claim_prefix'):
   segs=[s for s in segs if s['claim_id'].startswith(case['target_claim_prefix'])]
  if not segs or any(s.get('step_format')!='source_paragraph' for s in segs):errors.append('source_paragraph')
 if case.get('branches'):
  conditions={b['condition'] for c in contexts for s in c.get('rotation_segments',[]) for b in s.get('branches',[])}
  if not set(case['branches'])<=conditions:errors.append('missing_alternative_branch')
 if case.get('local_uncovered') and not any(c.get('uncovered_local_rotation_request') for c in contexts):errors.append('missing_local_scope_explanation')
 if case.get('empty'):
  if result.get('status')!='no_reviewed_match':errors.append('version_scope')
  return errors
 if case.get('no_context'):
  if any(contexts):errors.append('scope_leak')
  return errors
 if case.get('phase_uncovered'):
  if result.get('status')=='no_reviewed_match':return errors
  explanation='unexpanded_cycle_request' if case.get('cycle_unexpanded') else 'uncovered_phase_request'
  if not any(c.get(explanation) for c in contexts):errors.append('missing_phase_explanation')
  if any(c.get('rotation_segments') for c in contexts):errors.append('unlabelled_flow_promoted')
  return errors
 if case.get('uncovered'):
  if any(c.get('rotation_segments') for c in contexts):errors.append('unknown_chain_leak')
  if any(c and not c.get('uncovered_rotation_request') for c in contexts):errors.append('missing_scope_explanation')
  return errors
 if result.get('status')!='found' or not any(contexts):errors.append('missing_curated_evidence')
 for c in contexts:
  if c and c.get('scope')!='this_source_revision_only':errors.append('unscoped_terms')
 names={t['name'] for c in contexts for t in c.get('terms',[])}
 if not set(case.get('terms',[]))<=names:errors.append('missing_term')
 if case.get('unresolved') and not any(case['unresolved'] in c.get('unresolved_terms',[]) for c in contexts):errors.append('pending_term')
 if case.get('mode'):
  segs=[s for c in contexts for s in c.get('rotation_segments',[])]
  actual={(s['mode'],s['phase']) for s in segs};expected={(case['mode'],p) for p in case['phases']}
  covered=(expected<=actual and all(mode==case['mode'] for mode,_ in actual)) if allow_context_phases else actual==expected
  if not covered:errors.append('rotation_coverage')
  if case.get('chain_scope') and any(s['chain_scope']!=case['chain_scope'] for s in segs):errors.append('chain_scope')
 if case.get('dependency_target'):
  refs=[r for c in contexts for seg in c.get('rotation_segments',[]) for r in seg.get('dependencies',[])]
  if not any(r['target_phase']==case['dependency_target'] and r.get('included_in_claim') and r.get('source_excerpt_complete') for r in refs):errors.append('missing_dependency_excerpt')
 if case.get('owner_range'):
  expected=case['owner_range']
  if not all([seg.get('owner_chain_scope',{}).get('min'),seg.get('owner_chain_scope',{}).get('max')]==expected for c in contexts for seg in c.get('rotation_segments',[])):errors.append('owner_range')
 if case.get('teammate_condition') and not any(t['text']==case['teammate_condition'] for c in contexts for t in c.get('teammate_chain_conditions',[])):errors.append('missing_teammate_condition')
 for text in case.get('required_text',[]):
  if text not in json.dumps(relevant,ensure_ascii=False):errors.append('missing_source_condition:'+text)
 if check_budget and len(json.dumps(result,ensure_ascii=False))>8000:errors.append('budget')
 return errors


def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--live',action='store_true');ap.add_argument('--case',action='append');ap.add_argument('--output',required=True);args=ap.parse_args()
 out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
 report={'schema':'guide-curation-acceptance-v1','cases':[],'note':'Checks cover delivered source, phases and terminology; final prose still requires manual comparison with source steps.'}
 system=build_system_prompt(SimpleNamespace(**json.loads((ROOT/'backend/characters/amis.json').read_text(encoding='utf-8'))))
 for case in CASES:
  if args.case and case['id'] not in args.case:continue
  result=search_documents(case['query'],category='guide',entity_name=case.get('entity','爱弥斯'),game_version=case.get('game_version'))
  item={**case,'evidence':result,'errors':evidence_check(case,result),'answer':'','calls':[],'traces':[],'intent':None}
  if args.live:
   original=orchestrator.execute_tool;sources=[]
   def execute(name,arguments):
    response=original(name,arguments);item['calls'].append({'name':name,'arguments':json.loads(arguments),'result':json.loads(response)});return response
   try:
    with patch.object(orchestrator,'execute_tool',side_effect=execute):
     for event in orchestrator.run_agent([{'role':'system','content':system},*case.get('history',[]),{'role':'user','content':case['question']}],tools=tools.TOOLS,intent_routing=True):
      if event['type']=='delta':item['answer']+=event['content']
      elif event['type']=='intent_trace':item['intent']=event['trace']
      elif event['type']=='knowledge_trace':item['traces'].append(event['trace']);sources.extend(event['sources'])
    item['answer']=_grounded_text(item['answer'])
    item['answer']+=_source_suffix(sources,item['answer'])
    docs=[call['result'] for call in item['calls'] if call['name']=='search_documents']
    if not docs:item['errors'].append('no_live_document_call')
    # The model may fetch phases in separate calls. Assess coverage across calls,
    # while enforcing the tool budget independently for every response.
    combined={'status':'found' if any(d.get('status')=='found' for d in docs) else 'no_reviewed_match',
              'results':[p for d in docs for p in d.get('results',[])]}
    if case.get('uncovered'):
     # A broad follow-up can inspect the source's actual scope. It must not be
     # judged as if its query requested zero-chain; the answer must stay unknown.
     pattern=r'完整.*轴' if case.get('local_uncovered') else r'[四4]链' if case['id']=='feixue_four_inline' else r'[零0]链'
     scoped=[call for call in item['calls'] if call['name']=='search_documents' and re.search(pattern,call['arguments'].get('query',''))]
     # For a question about a literal inline annotation, inspecting that exact
     # source is also sufficient. It establishes no owner-chain applicability.
     annotation_checked=case.get('action_annotation') and any(
      any(a['quote']==case['action_annotation'] for p in d.get('results',[]) for a in p.get('guide_context',{}).get('action_chain_annotations',[])) for d in docs)
     if not scoped and not annotation_checked:item['errors'].append('no_explicit_chain_check')
     for call in scoped:item['errors']+=evidence_check(case,call['result'])
     uncertain=bool(re.search(r'无法.{0,8}确认|不能确认|未能确认',item['answer'])) if case.get('local_uncovered') else '无法确认' in item['answer']
     if not uncertain:item['errors'].append('answer_missing_uncertainty')
    elif case.get('phase_uncovered'):
     scoped=[call for call in item['calls'] if call['name']=='search_documents' and re.search(r'启动轴|循环轴',call['arguments'].get('query',''))]
     partial_checked=case.get('cycle_unexpanded') and any(
      r.get('kind')=='partial_ellipsis' and r.get('expanded') is False
      for d in docs for p in d.get('results',[]) if not case.get('primary_source') or p.get('document_metadata',{}).get('source_id')==case['primary_source']
      for r in p.get('guide_context',{}).get('cycle_references',[]))
     if not scoped and not partial_checked:item['errors'].append('no_explicit_phase_check')
     for call in scoped:item['errors']+=evidence_check(case,call['result'])
    else:item['errors']+=evidence_check(case,combined,check_budget=False,allow_context_phases=case.get('allow_context_phases',False))
    if any(len(json.dumps(d,ensure_ascii=False))>8000 for d in docs):item['errors'].append('budget')
    for name in case.get('terms',[]):
     if name not in item['answer']:item['errors'].append('answer_term')
    for phrase in case.get('forbidden_answer',[]):
     if phrase in item['answer']:item['errors'].append('unsupported_answer:'+phrase)
    for phase in case.get('answer_phases',case.get('phases',[])):
     if phase not in item['answer']:item['errors'].append('answer_phase')
   except Exception as exc:item['errors'].append(type(exc).__name__)
  item['passed']=not item['errors'];report['cases'].append(item)
  out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps({'id':case['id'],'passed':item['passed'],'errors':item['errors']},ensure_ascii=False),flush=True)
 report['passed']=bool(report['cases']) and all(c['passed'] for c in report['cases']);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 return 0 if report['passed'] else 1


if __name__=='__main__':
 sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
