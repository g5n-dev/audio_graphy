import { expect, test, type Page } from "@playwright/test";

async function authenticate(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("ag_access_token", "demo-access-token");
    localStorage.setItem("ag_refresh_token", "demo-refresh-token");
    localStorage.setItem(
      "ag_user_info",
      JSON.stringify({
        id: 1,
        name: "演示管理员",
        email: "demo@audiography.cn",
        role: "admin",
        tenant_id: "tenant-demo",
      }),
    );
  });
}

test("候选接受后可预览、异步发布真实音轨并完成可审计对话编辑", async ({
  page,
}) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await authenticate(page);

  await page.goto("/#/receptions");
  await page.getByLabel("候选门店").fill("上海静安旗舰店");
  await page.getByRole("button", { name: "扫描候选" }).click();
  await expect(
    page.getByRole("button", { name: "接受并创建接待" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "接受并创建接待" }).click();
  await expect(page).toHaveURL(/#\/receptions\/101\/workspace$/);
  await expect(
    page.getByRole("heading", { name: "接待调听工作台" }),
  ).toBeVisible();

  const player = page.getByLabel("接待录音播放器");
  await expect(player).toHaveAttribute("src", /recordings\/5001\/audio/);
  await expect
    .poll(() =>
      player.evaluate((audio: HTMLAudioElement) => audio.duration),
    )
    .toBeCloseTo(105, 1);

  const sourceGrant = await page.request.get(
    "/api/v1/receptions/101/recordings/5001/audio?grant=demo-source-5001",
    { headers: { Range: "bytes=0-43" } },
  );
  expect(sourceGrant.status()).toBe(206);
  expect(sourceGrant.headers()["x-audio-valid-source-range-ms"]).toBe(
    "0-105000",
  );
  const sourceHeader = await sourceGrant.body();
  expect(sourceHeader.subarray(0, 4).toString("ascii")).toBe("RIFF");
  expect(sourceHeader.subarray(8, 12).toString("ascii")).toBe("WAVE");

  await player.evaluate((audio: HTMLAudioElement) => {
    audio.currentTime = 105;
    audio.dispatchEvent(new Event("timeupdate"));
  });
  await expect(player).toHaveAttribute("src", /recordings\/5002\/audio/);

  const secondGap = page.getByLabel("录音 #5002前静音空档（毫秒）");
  await secondGap.fill("1500");
  await page.getByRole("button", { name: "生成合并预览" }).click();
  await expect(page.getByText(/时间线 revision 5/)).toBeVisible();
  await expect(page.getByText("可生成物理音频")).toBeVisible();

  await page.getByRole("button", { name: "提交音频任务" }).click();
  await expect(page.getByText(/任务 #\d+ · succeeded · 100%/)).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText(/接待 #101 .* v5/)).toBeVisible();
  await page.getByRole("button", { name: /合并接待音轨/ }).click();
  await expect(player).toHaveAttribute("src", /\/receptions\/101\/audio\?artifact=op-/);

  const mergedUrl = await player.getAttribute("src");
  expect(mergedUrl).not.toBeNull();
  const mergedHeaderResponse = await page.request.get(mergedUrl!, {
    headers: { Range: "bytes=0-43" },
  });
  expect(mergedHeaderResponse.status()).toBe(206);
  const mergedHeader = await mergedHeaderResponse.body();
  const sampleRate = mergedHeader.readUInt32LE(24);
  const dataBytes = mergedHeader.readUInt32LE(40);
  expect(sampleRate).toBe(16_000);
  expect(dataBytes / 2 / sampleRate).toBeCloseTo(423.9, 3);

  await page.getByLabel("对话编辑原因").fill("E2E 校验边界与溯源");
  await page
    .getByRole("button", {
      name: /定位转写 00:19 .*预算两万元/,
    })
    .click();
  await page.getByRole("button", { name: "在当前播放点切分" }).click();
  await expect(
    page.getByText("对话切分已保存，并写入审计记录。", { exact: true }),
  ).toBeVisible();

  const topicTrack = page.getByRole("group", { name: "主题轨道" });
  await topicTrack
    .getByRole("button", { name: /定位 迎宾开场/ })
    .evaluate((element: HTMLElement) => element.click());
  await topicTrack
    .getByRole("button", { name: /定位 需求探索/ })
    .evaluate((element: HTMLElement) => element.click());
  await expect(page.getByText("已选 2 个语义单元")).toBeVisible();
  await page.getByRole("button", { name: "合并相邻对话" }).click();
  await expect(
    page.getByText("相邻对话单元已合并，并写入审计记录。", {
      exact: true,
    }),
  ).toBeVisible();

  await expect(
    page.getByRole("heading", { name: "审计记录" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /定位证据 stage 38\.0 秒/ })
    .first()
    .click();
  await expect(page.locator(".ag-player__meta strong")).toContainText("00:38");
  expect(pageErrors).toEqual([]);
});
