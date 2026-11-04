"use strict";

const windowName = n => n ? "第 "+n+" 窗" : "开局";

function watchFinding(row) {
  if (row.status==="violated") return windowName(row.first_miss.window)+"首次记录到不满足";
  if (row.status==="observed") return windowName(row.first_hit.window)+"首次记录达成"+(row.matches===false ? "，观察终点已不满足" : row.matches===null ? "，终点数值未知" : "");
  const messages={tracking:row.changes+" 次已提交变化",pending:"截至此窗仍在观察",not_observed:"观察期内未出现目标结果",
    undetermined:"有窗口的值未知，无法判定整个观察期",ended_early:"运行在期限前结束，记录未覆盖整个期限",
    holding:"截至此窗始终满足，继续观察",maintained:"整个观察期始终满足",pending_end:"指定终点尚未到达",
    met_at_end:"观察终点满足条件",missed_at_end:"观察终点不满足条件",undetermined_end:"终点的值未知"};
  return messages[row.status] || "";
}
