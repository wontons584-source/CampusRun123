/**
 * 发单页（`/create`）。
 *
 * 字段与后端 `CreateOrderRequest` 一一对应：
 * type / from_poi_id / to_poi_id / reward(2–50, 0.5 步进) / expect_minutes(30|60|120|720) /
 * description / images / pick_code（仅 EXPRESS 必填）。
 */

import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import { isAuthError, toErrorMessage } from '../api/errors';
import { AppShell } from '../components/AppShell';
import { useToast } from '../components/Toast';
import { Button, Card, Field, Select, TextInput } from '../components/ui';
import { useAuth } from '../state/AuthContext';
import type { OrderType, Poi } from '../types';

type ExpectMinutes = 30 | 60 | 120 | 720;

/** 任务类型选项。 */
const TYPE_OPTIONS: ReadonlyArray<{ value: OrderType; label: string; hint: string }> = [
  { value: 'EXPRESS', label: '快递代取', hint: '帮取快递/外卖' },
  { value: 'BUY', label: '代买', hint: '食堂/超市代买' },
  { value: 'SEND', label: '代送', hint: '顺路送个东西' },
];

/** 期望时长选项。`720` 表示「今日内」。 */
const EXPECT_OPTIONS: ReadonlyArray<{ value: ExpectMinutes; label: string }> = [
  { value: 30, label: '30 分钟' },
  { value: 60, label: '1 小时' },
  { value: 120, label: '2 小时' },
  { value: 720, label: '今日内' },
];

const REWARD_MIN = 2;
const REWARD_MAX = 50;
const REWARD_STEP = 0.5;

/** 把数字夹在 [min,max] 并对齐到 0.5 步进，避免浮点尾巴。 */
function normalizeReward(value: number): number {
  const clamped = Math.min(REWARD_MAX, Math.max(REWARD_MIN, value));
  return Math.round(clamped / REWARD_STEP) * REWARD_STEP;
}

/** 发单页。 */
export function CreatePage(): JSX.Element {
  const navigate = useNavigate();
  const toast = useToast();
  const { logout } = useAuth();

  const [pois, setPois] = useState<Poi[]>([]);
  const [poisLoading, setPoisLoading] = useState<boolean>(true);

  const [type, setType] = useState<OrderType>('EXPRESS');
  const [fromPoiId, setFromPoiId] = useState<string>('');
  const [toPoiId, setToPoiId] = useState<string>('');
  const [rewardText, setRewardText] = useState<string>('5');
  const [expectMinutes, setExpectMinutes] = useState<ExpectMinutes>(30);
  const [description, setDescription] = useState<string>('');
  const [pickCode, setPickCode] = useState<string>('');
  const [submitting, setSubmitting] = useState<boolean>(false);

  const handleAuthFailure = (error: unknown): void => {
    if (isAuthError(error)) {
      logout();
      navigate('/login', { replace: true });
      return;
    }
    toast.error(toErrorMessage(error));
  };

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await api.pois();
        if (!cancelled) {
          setPois(list);
        }
      } catch (error) {
        if (!cancelled) {
          handleAuthFailure(error);
        }
      } finally {
        if (!cancelled) {
          setPoisLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // 只跑一次：handleAuthFailure 每次渲染都是新函数，故意不放进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const poiOptions = useMemo(
    () => pois.map((poi) => ({ value: `${poi.id}`, label: poi.name })),
    [pois],
  );

  const adjustReward = (delta: number): void => {
    const current = Number.parseFloat(rewardText);
    const base = Number.isFinite(current) ? current : REWARD_MIN;
    setRewardText(`${normalizeReward(base + delta)}`);
  };

  const validate = (): string | null => {
    if (fromPoiId === '') {
      return '请选择起点';
    }
    if (toPoiId === '') {
      return '请选择终点';
    }
    if (fromPoiId === toPoiId) {
      return '起点和终点不能相同';
    }
    const reward = Number.parseFloat(rewardText);
    if (!Number.isFinite(reward)) {
      return '请填写正确的酬劳金额';
    }
    if (reward < REWARD_MIN || reward > REWARD_MAX) {
      return `酬劳需在 ${REWARD_MIN}–${REWARD_MAX} 元之间`;
    }
    if (Math.abs(reward * 2 - Math.round(reward * 2)) > 1e-9) {
      return '酬劳需以 0.5 元为步进';
    }
    if (type === 'EXPRESS') {
      const code = pickCode.trim();
      if (code.length < 4 || code.length > 20) {
        return '取件码需 4–20 位';
      }
    }
    if (description.length > 200) {
      return '描述最多 200 字';
    }
    return null;
  };

  const handleSubmit = async (): Promise<void> => {
    if (submitting) {
      return;
    }
    const problem = validate();
    if (problem !== null) {
      toast.error(problem);
      return;
    }

    setSubmitting(true);
    try {
      const result = await api.createOrder({
        type,
        from_poi_id: Number(fromPoiId),
        to_poi_id: Number(toPoiId),
        reward: Number.parseFloat(rewardText),
        expect_minutes: expectMinutes,
        ...(description.trim() === '' ? {} : { description: description.trim() }),
        ...(type === 'EXPRESS' ? { pick_code: pickCode.trim() } : {}),
      });
      toast.success('发布成功，等待同学接单');
      navigate(`/orders/${result.id}`, { replace: true });
    } catch (error) {
      handleAuthFailure(error);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AppShell title="发布任务" showBack>
      <div className="space-y-4">
        {/* 任务类型 */}
        <Card>
          <Field label="任务类型" required>
            <div className="grid grid-cols-3 gap-2">
              {TYPE_OPTIONS.map((option) => {
                const active = type === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setType(option.value)}
                    className={`rounded-lg border px-2 py-3 text-center ${
                      active
                        ? 'border-primary bg-primary-light text-primary'
                        : 'border-gray-200 bg-white text-gray-600'
                    }`}
                  >
                    <span className="block text-sm font-medium">{option.label}</span>
                    <span className="mt-0.5 block text-[11px] text-gray-400">{option.hint}</span>
                  </button>
                );
              })}
            </div>
          </Field>
        </Card>

        {/* 路线 */}
        <Card>
          <div className="space-y-4">
            <Field label="起点" required htmlFor="from-poi">
              <Select
                id="from-poi"
                value={fromPoiId}
                options={poiOptions}
                placeholder={poisLoading ? '地点加载中…' : '请选择起点地点'}
                disabled={poisLoading}
                onChange={setFromPoiId}
              />
            </Field>

            <Field label="终点" required htmlFor="to-poi">
              <Select
                id="to-poi"
                value={toPoiId}
                options={poiOptions}
                placeholder={poisLoading ? '地点加载中…' : '请选择终点地点'}
                disabled={poisLoading}
                onChange={setToPoiId}
              />
            </Field>
          </div>
        </Card>

        {/* 酬劳与时长 */}
        <Card>
          <div className="space-y-4">
            <Field label="酬劳（元）" required hint="2–50 元，0.5 元步进">
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  aria-label="减少 0.5 元"
                  onClick={() => adjustReward(-REWARD_STEP)}
                  className="h-11 w-11 shrink-0 rounded-lg border border-gray-300 bg-white text-lg text-gray-600 active:bg-gray-100"
                >
                  −
                </button>
                <div className="flex-1">
                  <TextInput
                    id="reward"
                    value={rewardText}
                    inputMode="decimal"
                    onChange={setRewardText}
                  />
                </div>
                <button
                  type="button"
                  aria-label="增加 0.5 元"
                  onClick={() => adjustReward(REWARD_STEP)}
                  className="h-11 w-11 shrink-0 rounded-lg border border-gray-300 bg-white text-lg text-gray-600 active:bg-gray-100"
                >
                  +
                </button>
              </div>
            </Field>

            <Field label="期望时长" required>
              <div className="grid grid-cols-4 gap-2">
                {EXPECT_OPTIONS.map((option) => {
                  const active = expectMinutes === option.value;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => setExpectMinutes(option.value)}
                      className={`rounded-lg border py-2 text-sm ${
                        active
                          ? 'border-primary bg-primary-light font-medium text-primary'
                          : 'border-gray-200 bg-white text-gray-600'
                      }`}
                    >
                      {option.label}
                    </button>
                  );
                })}
              </div>
            </Field>
          </div>
        </Card>

        {/* 详情 */}
        <Card>
          <div className="space-y-4">
            {type === 'EXPRESS' ? (
              <Field label="取件码" required htmlFor="pick-code" hint="4–20 位，只有你能看到">
                <TextInput
                  id="pick-code"
                  value={pickCode}
                  placeholder="例如：8-2-3045"
                  maxLength={20}
                  onChange={setPickCode}
                />
              </Field>
            ) : null}

            <Field label="描述（选填）" htmlFor="description" hint={`${description.length}/200`}>
              <textarea
                id="description"
                value={description}
                maxLength={200}
                rows={3}
                placeholder="补充一点细节，接单更快：物品大小、联系方式、交付方式…"
                onChange={(event) => setDescription(event.target.value)}
                className="w-full resize-none rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-base text-gray-900 placeholder:text-gray-400 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20"
              />
            </Field>
          </div>
        </Card>

        <Button block size="lg" loading={submitting} onClick={() => void handleSubmit()}>
          发布任务
        </Button>
        <p className="pb-2 text-center text-xs text-gray-400">
          发布后订单进入抢单大厅，被接单后不可随意取消
        </p>
      </div>
    </AppShell>
  );
}
