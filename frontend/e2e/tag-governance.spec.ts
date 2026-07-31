import { expect, test, type Page } from "@playwright/test";

async function authenticate(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("ag_access_token", "demo-access-token");
    localStorage.setItem("ag_refresh_token", "demo-refresh-token");
    localStorage.setItem(
      "ag_user_info",
      JSON.stringify({
        id: 1,
        name: "张明",
        email: "demo@audiography.cn",
        role: "admin",
        tenant_id: "tenant-demo",
      }),
    );
  });
}

test.beforeEach(async ({ page }) => {
  await authenticate(page);
});

test("管理员可在治理中心查看版本、评估、发布与审计", async ({ page }) => {
  await page.goto("/#/tag-governance");

  await expect(
    page.getByRole("heading", { name: "标签治理中心" }),
  ).toBeVisible();
  const tabs = page.getByRole("tablist", { name: "标签治理视图" });
  await expect(tabs.getByRole("tab")).toHaveCount(6);
  await tabs.getByRole("tab", { name: "评估实验" }).click();
  await expect(page.getByText("Macro F1")).toBeVisible();
  await tabs.getByRole("tab", { name: "发布监控" }).click();
  await expect(page.getByText(/灰度流量|全量生产/)).toBeVisible();
  await expect(page.getByText("可信 Monitor 自动晋级")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /推进部署|推进至/ }),
  ).toHaveCount(0);
  await tabs.getByRole("tab", { name: "自进化" }).click();
  await expect(page.getByText("语义标签 Harness 自进化")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /启动优化/ }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: /启动优化/ }).click();
  const optimizationDialog = page.getByRole("dialog", {
    name: "启动自进化优化",
  });
  await expect(optimizationDialog).toBeVisible();
  await optimizationDialog
    .getByRole("button", { name: "启动优化运行" })
    .click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: /优化运行 #\d+ 已完成（演示数据）/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /hybrid-v3\.2-evolved-\d+/ }).first(),
  ).toBeVisible();
  await tabs.getByRole("tab", { name: "审计" }).click();
  await expect(page.getByText(/tagger|schema|deployment/i).first()).toBeVisible();
});

test("复核决定会写入人工事实并提供证据回听", async ({ page }) => {
  const batch = await page.request.post("/api/v1/tag-reviews/create-batch", {
    headers: {
      Authorization: "Bearer demo-access-token",
      "Content-Type": "application/json",
    },
    data: {
      reason: "conflict",
      subjects: [
        {
          subject_type: "dialogue_unit",
          subject_id: 1006,
          reception_id: 101,
          tag_key: "objection",
          proposed_value: "价格敏感",
          schema_version_id: 11,
          tagger_version_id: 21,
          evidence_refs: [
            {
              recording_id: 5003,
              start_sec: 278,
              end_sec: 286,
              text_excerpt:
                "这个价格确实超出我们的预算了，能不能再优惠一点？",
            },
          ],
        },
      ],
    },
  });
  expect(batch.ok()).toBeTruthy();
  await page.goto("/#/tag-review");

  await expect(
    page.getByRole("heading", { name: "人工复核工作台" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /跳转调听/ })).toBeVisible();
  const claim = page.getByRole("button", {
    name: "领取任务",
    exact: true,
  });
  if (await claim.isVisible()) {
    await claim.click();
  }
  await page.getByRole("radio", { name: "纠正标签" }).click();
  const correctedValue = page.getByLabel("纠正后的标签值");
  await expect(correctedValue).toBeVisible();
  if ((await correctedValue.evaluate((element) => element.tagName)) === "SELECT") {
    await correctedValue.selectOption({ index: 1 });
  } else {
    await correctedValue.fill("价格异议");
  }
  await page
    .getByRole("combobox", { name: "主要错误层" })
    .selectOption("tag_reasoning");
  await page
    .getByRole("combobox", { name: "复核置信度" })
    .selectOption("0.9");
  await page.getByRole("checkbox", { name: "证据确认" }).check();
  await page
    .getByRole("textbox", { name: "复核备注" })
    .fill("客户明确表示今天签约");
  await page.getByRole("button", { name: "提交复核" }).click();
  await expect(page.getByText("复核已写入人工事实")).toBeVisible();
});

test("标签洞察使用真 Tab 且切换不滚动、不改路由", async ({ page }) => {
  await page.goto("/#/tag-insights");

  const tabs = page.getByRole("tablist", { name: "标签洞察视图" });
  await expect(tabs.getByRole("tab")).toHaveCount(3);
  await expect(
    tabs.getByRole("tab", { name: "关系图谱" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("tabpanel", { name: "关系图谱" }),
  ).toBeVisible();
  await expect(page.getByRole("tabpanel")).toHaveCount(1);

  const originalUrl = page.url();
  await page.evaluate(() => window.scrollTo(0, 0));
  await tabs
    .getByRole("tab", { name: "对比矩阵" })
    .evaluate((element: HTMLElement) => element.click());
  await expect(
    tabs.getByRole("tab", { name: "对比矩阵" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("tabpanel", { name: "对比矩阵" }),
  ).toBeVisible();
  await expect(page.getByRole("tabpanel")).toHaveCount(1);
  await expect(page).toHaveURL(originalUrl);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);

  await tabs
    .getByRole("tab", { name: "对比矩阵" })
    .evaluate((element: HTMLElement) => element.focus({ preventScroll: true }));
  await page.keyboard.press("End");
  await expect(
    tabs.getByRole("tab", { name: "图表分析" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("tabpanel", { name: "图表分析" }),
  ).toBeVisible();
  await expect(page.getByRole("tabpanel")).toHaveCount(1);
  await expect(page).toHaveURL(originalUrl);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});

test("全域图谱使用真实 Tab，旧社区地址跳转到主题聚类", async ({
  page,
}) => {
  await page.goto("/#/communities");

  await expect(page).toHaveURL(/#\/graph\?view=clusters$/);
  const tabs = page.getByRole("tablist", { name: "全域知识图谱视图" });
  await expect(
    tabs.getByRole("tab", { name: "主题聚类" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel", { name: "主题聚类" })).toBeVisible();
  await expect(
    page.getByRole("tabpanel", { name: "实体关系" }),
  ).toHaveCount(0);

  const topicTab = tabs.getByRole("tab", { name: "主题聚类" });
  await topicTab.focus();
  await page.keyboard.press("Home");
  await expect(
    tabs.getByRole("tab", { name: "实体关系" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel", { name: "实体关系" })).toBeVisible();
  await expect(
    page.getByRole("tabpanel", { name: "主题聚类" }),
  ).toHaveCount(0);
});
