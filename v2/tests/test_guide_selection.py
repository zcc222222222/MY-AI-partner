"""Recommendation hierarchy and topic coverage must survive bounded excerpts."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from llmwiki.guide_selection import select_claims
from llmwiki.search import terms


class GuideSelectionTests(unittest.TestCase):
    def select(self,query,rows):
        claims=[{'claim_id':str(i),'text':text,'citations':[{'source_path':path}]} for i,(_,text,path) in enumerate(rows)]
        headings={str(i):heading for i,(heading,_,_) in enumerate(rows)}
        result=select_claims({'claims':claims},claims,headings,query,terms(query))
        return result

    def test_weapon_summary_and_alternative_are_returned_together_not_team(self):
        rows=[('配队推荐','作者推荐角色甲与角色乙搭配。',[]),('武器选择','武器主推刀甲，下位替代刀乙作为过渡。',['content','weaponTexts']),
              ('武器选择','刀乙增加攻击和共鸣效率。',['content','weaponTexts'])]
        claims,topics=self.select('武器推荐',rows)
        self.assertEqual(topics,['weapon'])
        self.assertEqual(claims[0]['claim_id'],'1')
        self.assertEqual({c['claim_id'] for c in claims},{'1','2'})

    def test_generic_build_covers_weapon_echo_and_team_before_filling_details(self):
        rows=[('武器选择',f'武器推荐刀{i}。',['content','weaponTexts']) for i in range(6)]
        rows += [('声骸推荐','声骸首选套装甲。',['content','echoTexts']),('配队推荐','队伍首选角色乙。',['content','teammate'])]
        claims,topics=self.select('怎么养成',rows)
        self.assertEqual(topics,['weapon','echo','team'])
        self.assertEqual({c['claim_id'] for c in claims[:3]},{'0','6','7'})
        claims,topics=self.select('攻略',rows)
        self.assertEqual(topics,['weapon','echo','team'])
        self.assertEqual({c['claim_id'] for c in claims[:3]},{'0','6','7'})

    def test_team_mode_and_negative_prerequisite_are_preserved(self):
        rows=[('配队推荐','震谐首选甲乙丙，不推荐没有乙的震谐队。',[]),('配队推荐','聚爆首选甲丁戊。',[]),('配队推荐','震谐对单可用甲乙丙。',[])]
        claims,_=self.select('没有乙怎么配队 震谐',rows)
        self.assertEqual(claims[0]['claim_id'],'0')
        self.assertFalse(any('聚爆' in c['text'] for c in claims))
        rows=[('配队推荐','震谐首选甲、乙、丙。',['content','baseTexts']),('配队推荐','聚爆首选甲、丁、戊。',['content','baseTexts']),
              ('配队推荐','角色乙的快速处决配合角色丙可提高收益。',['content','baseTexts']),
              ('配队推荐','角色丁配合角色戊施加效应。',['content','baseTexts'])]
        claims,_=self.select('配队 震谐 聚爆',rows)
        self.assertIn('角色乙',claims[2]['text'])

    def test_echo_question_with_weapon_condition_does_not_switch_to_weapons(self):
        rows=[('武器选择','专武首选刀甲。',['content','weaponTexts']),('声骸推荐','带专武时声骸主词条选暴击伤害。',['content','echoTexts'])]
        claims,topics=self.select('带专武声骸词条怎么选',rows)
        self.assertEqual(topics,['echo'])
        self.assertEqual([c['claim_id'] for c in claims],['1'])

    def test_mixed_heading_does_not_turn_echo_into_weapon_advice(self):
        rows=[('武器与声骸','声骸只推荐套装甲。',['content','echoTexts']),('武器与声骸','武器主推刀甲。',['content','weaponTexts'])]
        claims,_=self.select('武器推荐',rows)
        self.assertEqual([c['claim_id'] for c in claims],['1'])

    def test_named_candidate_can_select_its_source_topic_without_weapon_keyword(self):
        rows=[('推荐','界面武器候选是刀甲与刀乙。',['content','weapon','items']),('武器选择','主推刀甲，下位替代刀乙。',[])]
        claims,topics=self.select('刀乙能用吗',rows)
        self.assertEqual(topics,['weapon'])
        self.assertIn('下位替代',claims[0]['text'])

    def test_specific_teammate_detail_is_not_buried_by_generic_summaries(self):
        rows=[('配队推荐',f'首选模式{i}的组合。',[]) for i in range(7)]
        rows += [('配队推荐','界面推荐同一槽位的候选为角色乙与角色丙。',[])]
        claims,_=self.select('角色丙能替换角色乙配队吗',rows)
        self.assertIn('角色丙',claims[0]['text'])

    def test_non_guide_topic_falls_back_without_manufacturing_evidence(self):
        self.assertIsNone(self.select('角色的生日', [('档案','生日未知。',[])]))

    def test_unsupported_notification_is_removed_but_limitation_and_reactive_help_survive(self):
        from app.services.chat_service import _grounded_text
        text='指定版本暂无资料。别急，等我这边资料齐了，第一个告诉你，好不好？'
        self.assertEqual(_grounded_text(text),'指定版本暂无资料。')
        valid='我不能主动通知你。你再来问时，我会按当时资料回答。'
        self.assertEqual(_grounded_text(valid),valid)


if __name__=='__main__':unittest.main()
