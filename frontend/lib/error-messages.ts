// 错误码 → 用户友好文案的映射(纯函数,无依赖)。
// 覆盖三类输入:
//   1. core ErrorCode(后端 RunError.error_code 可能值);
//   2. ApiErrorCode(前端 ApiClientError.code 可能值);
//   3. null / undefined(网络失败、超时等请求层错误)与未知字符串兜底。
export type ErrorMessagePreset = {
  /** 简短标题,如「会话正忙」 */
  title: string;
  /** 说明,如「该会话正在处理其他请求,请稍后重试。」 */
  detail: string;
  /** 操作建议,如「重新发送上一条消息」;无建议时省略 */
  action?: string;
};

export function errorMessageFor(
  code: string | null | undefined,
): ErrorMessagePreset {
  switch (code) {
    // —— ApiErrorCode:HTTP 层稳定错误 ——
    case "session_busy":
      return {
        title: "会话正忙",
        detail: "该会话正在处理其他请求,请稍后重试。",
        action: "稍后再试",
      };
    case "handoff_not_pending":
      return { title: "审批已处理", detail: "该审批已被处理或已过期。" };
    case "tool_approval_not_pending":
      return { title: "命令审批已失效", detail: "该命令审批已被处理或已过期。" };
    case "session_not_found":
      return {
        title: "会话不存在",
        detail: "该会话不存在或已被归档,请新建会话后重试。",
      };
    case "session_already_exists":
      return {
        title: "会话已存在",
        detail: "已存在同名会话,请更换名称后重试。",
      };
    case "invalid_request":
      return { title: "请求无效", detail: "请求参数不合法,请检查后重试。" };
    case "internal_error":
      return { title: "服务内部错误", detail: "服务开小差了,请稍后重试。" };

    // —— core ErrorCode:一次 run 内的执行错误 ——
    case "model_call_failed":
      return { title: "模型服务暂不可用", detail: "模型调用失败,请稍后重试。" };
    case "tool_timeout":
    case "tool_execution_failed":
      return { title: "工具执行异常", detail: "检索或工具调用未完成,请重试。" };
    case "tool_unknown":
      return { title: "工具不存在", detail: "请求了未知的工具,请稍后重试。" };
    case "tool_unauthorized":
      return { title: "工具未授权", detail: "当前没有调用该工具的权限。" };
    case "tool_invalid_arguments":
      return { title: "工具参数无效", detail: "工具调用参数不合法,请调整后重试。" };
    case "tool_no_progress":
      return { title: "工具调用无进展", detail: "相同工具调用被重复请求,本轮已停止。" };
    case "tool_budget_exceeded":
      return { title: "工具调用过多", detail: "本轮工具调用已达到安全上限。" };
    case "tool_approval_rejected":
      return { title: "命令已拒绝", detail: "命令未执行,Agent 将据此继续处理。" };
    case "tool_approval_queue_limit":
      return { title: "待审批命令过多", detail: "一次只能审批一条命令。" };
    case "react_iteration_limit":
    case "graph_handoff_limit":
    case "graph_switch_limit":
      return {
        title: "执行轮次超限",
        detail: "本轮协作步骤过多,已停止。请精简问题后重试。",
      };
    case "graph_invalid_target":
      return {
        title: "协作目标无效",
        detail: "协作流程的目标 Agent 无效,请检查后重试。",
      };
    case "graph_aggregation_invalid":
      return { title: "协作汇总异常", detail: "协作结果汇总失败,请稍后重试。" };
    case "agent_output_invalid":
      return {
        title: "Agent 输出无效",
        detail: "Agent 返回了无法解析的结果,请重试。",
      };

    // 请求层错误:网络失败 / 超时(ApiClientError.code === null)
    case null:
    case undefined:
      return {
        title: "网络请求失败",
        detail: "请检查网络连接后重试。",
        action: "重新发送上一条消息",
      };

    // 未知错误码兜底(后端或契约未来新增的码)
    default:
      return { title: "出了点问题", detail: "请求未能完成,请稍后重试。" };
  }
}
