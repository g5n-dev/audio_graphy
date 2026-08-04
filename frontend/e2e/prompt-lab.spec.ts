import { expect, test, type Page } from "@playwright/test";

async function authenticate(page: Page, role: "admin" | "agent" = "admin") {
  await page.addInitScript(
    (userRole) => {
      localStorage.setItem("ag_access_token", "demo-access-token");
      localStorage.setItem("ag_refresh_token", "demo-refresh-token");
      localStorage.setItem(
        "ag_user_info",
        JSON.stringify({
          id: 1,
          name: "张明",
          email: "demo@audiography.cn",
          role: userRole,
          tenant_id: "tenant-demo",
        }),
      );
    },
    role,
  );
}

test("管理员可从就绪度一路走到补丁决策", async ({ page }) => {
  await authenticate(page);
  await page.goto("/#/prompt-lab");

  await expect(page.getByRole("heading", { name: "提示词实验室" })).toBeVisible();
  const tabs = page.getByRole("tablist", { name: "提示词实验室视图" });
  await expect(tabs.getByRole("tab")).toHaveCount(5);

  // 就绪矩阵要能说出还差多少条，而不是只给一个数字。
  const matrix = page.getByRole("table", { name: "已复核样本覆盖矩阵" });
  await expect(matrix).toBeVisible();
  await expect(
    matrix.getByRole("cell", { name: /距门槛还差 \d+ 条/ }).first(),
  ).toBeVisible();

  await tabs.getByRole("tab", { name: /编译运行/ }).click();
  await expect(
    page.getByRole("button", { name: "查看产物 301 的差异" }),
  ).toBeVisible();

  await page.getByRole("button", { name: "查看产物 301 的差异" }).click();
  await expect(page.getByRole("heading", { name: "候选 Prompt" })).toBeVisible();
  await expect(page.getByText("预算内")).toBeVisible();

  // 归属重建在演示数据上必须成功，否则补丁标识不会出现。
  await expect(page.getByText(/^补丁 [0-9a-f]{8} · /).first()).toBeVisible();
  await expect(
    page.getByText("本次差异无法归属到具体补丁", { exact: false }),
  ).toHaveCount(0);

  await tabs.getByRole("tab", { name: /梯度与补丁/ }).click();
  await expect(page.getByText("① 失败样本").first()).toBeVisible();
  await expect(page.getByText("④ 应用后效果").first()).toBeVisible();
});

test("前置条件未满足时拒绝编译并说明原因", async ({ page }) => {
  // 演示数据刻意留了两个未达标组合，正好用来验证这条守卫。
  await authenticate(page);
  await page.goto("/#/prompt-lab?tab=compile");

  await expect(page.getByRole("button", { name: "发起编译" })).toBeDisabled();
  await expect(page.getByText(/编译前置条件尚未满足/)).toBeVisible();
});

test("就绪度页把阻塞项翻成人话并给出去处", async ({ page }) => {
  await authenticate(page);
  await page.goto("/#/prompt-lab");

  await expect(page.getByText(/的已复核样本不足/).first()).toBeVisible();
  await expect(
    page.getByRole("link", { name: "查看标签洞察" }).first(),
  ).toBeVisible();
  // 剩余工时是排期决策的依据，必须写明估算口径。
  await expect(page.getByText(/按每条 5 分钟估算/)).toBeVisible();
});

test("顾问被权限边界挡住并被引导到可访问的页面", async ({ page }) => {
  await authenticate(page, "agent");
  await page.goto("/#/prompt-lab");

  await expect(page.getByRole("alert", { name: "无标签治理权限" })).toBeVisible();

  const sidebar = page.getByRole("navigation", { name: "平台功能导航" });
  await expect(sidebar.getByText("提示词实验室")).toHaveCount(0);

  await page.getByRole("button", { name: "查看标签洞察" }).click();
  await expect(page).toHaveURL(/#\/tag-insights/);
});

test("标签页可用键盘操作且切换不改变路由", async ({ page }) => {
  await authenticate(page);
  await page.goto("/#/prompt-lab");

  const tabs = page.getByRole("tablist", { name: "提示词实验室视图" });
  await tabs.getByRole("tab", { name: /数据就绪/ }).focus();
  for (let i = 0; i < 4; i += 1) {
    await page.keyboard.press("ArrowRight");
  }

  await expect(tabs.getByRole("tab", { name: /回放对比/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page).toHaveURL(/#\/prompt-lab\?tab=replay/);
});
