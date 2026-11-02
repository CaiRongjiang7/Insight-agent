"""Evidence provenance and exploration rules shared by investigator and writer."""

from .retrospective_models import EvidenceSource

EXPLORATION_POLICY = (
    '群聊、日志是定位起点，不是影响范围的完整清单。保留日志所处环境/时间窗口，群聊保留参与者陈述身份；'
    '可靠性与覆盖度分开，未提及、未命中、无报错或预算耗尽均不代表未受影响。'
    '先定位已知症状，再按具体假设搜索调用方、被调用方、错误处理、配置和服务映射；'
    '有搜索工具时先找文件/字面量搜索，再按行读取；方法名唯一时再查直接调用关系。'
    '必要时读取 .insight/context.json 并检索依赖者及被依赖者，范围仅限当前仓库固定版本；'
    '映射缺失不代表没有其他服务。每个静态传播候选须说明成立条件及需要的日志/指标/Trace/部署版本验证，'
    '不要据静态路径断言事故发生时执行了该路径或扩大已证实影响。'
    '优先核对最可能改变结论的分支；不要无界遍历。资料足够或预算用尽即停止，保留未验证分支。'
    '工具若声明 no_collected_material 或 retry_with_new_query=false，改写查询不能补齐该来源，应停止对此来源反复查询。'
)


def material_role(source_type: EvidenceSource) -> str:
    return {
        EvidenceSource.FEISHU_MESSAGE: 'participant_statement',
        EvidenceSource.FEISHU_DOC: 'document_statement',
        EvidenceSource.REPOSITORY_CODE: 'static_candidate',
        EvidenceSource.GITHUB_DIFF: 'static_candidate',
        EvidenceSource.CI_LOG: 'log_observation',
        EvidenceSource.HISTORICAL: 'historical_analogy',
        EvidenceSource.MANUAL: 'manual_statement',
    }.get(source_type, 'document_statement')
