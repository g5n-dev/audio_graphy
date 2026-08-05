/**
 * 标签定义层次树编辑器 —— 建版本时替代手写 JSON。
 *
 * 层级来自后端契约本身,不是新造的概念:`category` 是分组,`depends_on` 与
 * `mutually_exclusive_with` 是标签间的关系。树按 category 分组展示,每个标签
 * 一张可展开的卡片。
 *
 * JSON 视图保留并双向同步。理由:版本发布后不可变,操作员常常从上一版复制
 * 粘贴再改——去掉粘贴入口会让这个编辑器变成倒退。树是默认视图,JSON 是
 * 逃生舱,两边任何时刻是同一份数据。
 */

import { useMemo, useState } from "react";
import type { TagDefinition } from "@/types/api";
import "./tagDefinitionTree.css";

const VALUE_TYPES = ["enum", "string", "number", "boolean"] as const;
const SUBJECT_TYPES = ["dialogue_unit", "reception"] as const;
const SUBJECT_LABEL: Record<string, string> = {
  dialogue_unit: "对话单元",
  reception: "整次接待",
};

function emptyDefinition(index: number): TagDefinition {
  return {
    key: `category.tag_${index}`,
    name: "",
    category: "category",
    value_type: "enum",
    allowed_values: [],
    subject_types: ["dialogue_unit"],
    scenarios: [],
    evidence_required: true,
    critical: false,
    required: false,
    threshold: 0.75,
  } as TagDefinition;
}

function DefinitionCard({
  definition,
  siblings,
  onChange,
  onRemove,
}: {
  definition: TagDefinition;
  /** 同版本内其他标签的 key,供依赖/互斥引用——不允许引用不存在的标签。 */
  siblings: string[];
  onChange: (next: TagDefinition) => void;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const patch = (fields: Partial<TagDefinition>) =>
    onChange({ ...definition, ...fields });

  const allowedValuesText = (definition.allowed_values ?? []).join(", ");

  return (
    <li className="ag-def-card">
      <div className="ag-def-card__head">
        <button
          type="button"
          className="ag-def-card__toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "▾" : "▸"}
        </button>
        <input
          aria-label={`标签键 ${definition.key}`}
          className="ag-def-card__key"
          value={definition.key}
          placeholder="intent.purchase"
          onChange={(event) => patch({ key: event.target.value })}
        />
        <input
          aria-label={`标签名称 ${definition.key}`}
          value={definition.name}
          placeholder="中文名,如 购买意向"
          onChange={(event) => patch({ name: event.target.value })}
        />
        <select
          aria-label={`值类型 ${definition.key}`}
          value={definition.value_type}
          onChange={(event) =>
            patch({
              value_type: event.target.value as TagDefinition["value_type"],
            })
          }
        >
          {VALUE_TYPES.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="ag-def-card__remove"
          aria-label={`删除标签 ${definition.key}`}
          onClick={onRemove}
        >
          ✕
        </button>
      </div>

      {expanded && (
        <div className="ag-def-card__body">
          {definition.value_type === "enum" && (
            <label>
              取值域(逗号分隔)
              <input
                aria-label={`取值域 ${definition.key}`}
                value={allowedValuesText}
                placeholder="low, medium, high"
                onChange={(event) =>
                  patch({
                    allowed_values: event.target.value
                      .split(",")
                      .map((value) => value.trim())
                      .filter(Boolean),
                  })
                }
              />
              {/* enum 没有取值域后端会拒绝——在这里说,不要等提交才报错。 */}
              <small>enum 必须给出取值域,否则版本无法提交。</small>
            </label>
          )}

          <label>
            适用主体
            <span className="ag-def-card__checks">
              {SUBJECT_TYPES.map((subject) => (
                <label key={subject}>
                  <input
                    type="checkbox"
                    checked={definition.subject_types.includes(subject)}
                    onChange={(event) =>
                      patch({
                        subject_types: event.target.checked
                          ? [...definition.subject_types, subject]
                          : definition.subject_types.filter(
                              (item) => item !== subject,
                            ),
                      })
                    }
                  />
                  {SUBJECT_LABEL[subject]}
                </label>
              ))}
            </span>
          </label>

          <label>
            自动采纳阈值
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              aria-label={`阈值 ${definition.key}`}
              value={definition.threshold}
              onChange={(event) =>
                patch({ threshold: Number(event.target.value) })
              }
            />
          </label>

          <label className="ag-def-card__flags">
            <span>
              <input
                type="checkbox"
                checked={definition.evidence_required}
                onChange={(event) =>
                  patch({ evidence_required: event.target.checked })
                }
              />
              必须带证据
            </span>
            <span>
              <input
                type="checkbox"
                checked={definition.critical}
                onChange={(event) => patch({ critical: event.target.checked })}
              />
              关键标签
            </span>
            <span>
              <input
                type="checkbox"
                checked={definition.required}
                onChange={(event) => patch({ required: event.target.checked })}
              />
              必填
            </span>
          </label>

          {siblings.length > 0 && (
            <label>
              依赖标签
              <select
                aria-label={`依赖标签 ${definition.key}`}
                multiple
                size={Math.min(4, siblings.length)}
                value={definition.depends_on ?? []}
                onChange={(event) =>
                  patch({
                    depends_on: Array.from(
                      event.target.selectedOptions,
                      (option) => option.value,
                    ),
                  })
                }
              >
                {siblings.map((key) => (
                  <option key={key} value={key}>
                    {key}
                  </option>
                ))}
              </select>
              {/* 只能选同版本内的标签:引用不存在的 key 是发布后才会炸的错。 */}
              <small>只能引用本版本内的其他标签。</small>
            </label>
          )}
        </div>
      )}
    </li>
  );
}

export function TagDefinitionTree({
  definitions,
  onChange,
}: {
  definitions: TagDefinition[];
  onChange: (next: TagDefinition[]) => void;
}) {
  const grouped = useMemo(() => {
    const map = new Map<string, { definition: TagDefinition; index: number }[]>();
    definitions.forEach((definition, index) => {
      // 用真实值分桶(含空串):把空串显示成「未分组」是展示层的事,
      // 混进输入框的 value 会让用户在占位名后面接着打字。
      const category = definition.category ?? "";
      const bucket = map.get(category) ?? [];
      bucket.push({ definition, index });
      map.set(category, bucket);
    });
    return [...map.entries()];
  }, [definitions]);

  /** 改组名按成员下标改,不按旧名回查:清空输入框会让 category 变成 "",
   *  再按名字匹配就找不回这些标签了(它们已落进「未分组」桶)。 */
  const renameGroup = (memberIndexes: number[], nextCategory: string) => {
    const members = new Set(memberIndexes);
    onChange(
      definitions.map((item, position) =>
        members.has(position) ? { ...item, category: nextCategory } : item,
      ),
    );
  };

  const replaceAt = (index: number, next: TagDefinition) =>
    onChange(definitions.map((item, position) => (position === index ? next : item)));

  return (
    <div className="ag-def-tree">
      {/* key 用位置而非 category:分组名是可编辑的,拿它当 key 会让每敲一个
          字就卸载重建整个分组,输入框每次都丢焦点。 */}
      {grouped.map(([category, items], groupIndex) => (
        <section key={groupIndex} className="ag-def-tree__group">
          <header>
            <input
              aria-label={`分组名 ${category || "未分组"}`}
              placeholder="未分组"
              value={category}
              onChange={(event) =>
                renameGroup(
                  items.map((item) => item.index),
                  event.target.value,
                )
              }
            />
            <span>{items.length} 个标签</span>
          </header>
          <ul>
            {items.map(({ definition, index }) => (
              <DefinitionCard
                key={index}
                definition={definition}
                siblings={definitions
                  .filter((_item, position) => position !== index)
                  .map((item) => item.key)
                  .filter(Boolean)}
                onChange={(next) => replaceAt(index, next)}
                onRemove={() =>
                  onChange(definitions.filter((_item, position) => position !== index))
                }
              />
            ))}
          </ul>
        </section>
      ))}
      <button
        type="button"
        className="ag-def-tree__add"
        onClick={() => onChange([...definitions, emptyDefinition(definitions.length + 1)])}
      >
        + 添加标签
      </button>
    </div>
  );
}
