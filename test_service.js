"use strict";

const { spawnSync } = require("node:child_process");

// 运行全部契约测试：基础服务 + credit 领域包（授信/提款/冲减/信号/催收/合规）
const result = spawnSync("python3", ["-m", "unittest", "-v",
  "service_contract",
  "credit.test_underwriting",
  "credit.test_creditline",
  "credit.test_monitoring_review",
  "credit.test_governance",
  "credit.test_http_api",
], { stdio: "inherit" });

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
