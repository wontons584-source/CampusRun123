/**
 * 登录 / 注册页（`/login`）。
 *
 * 同页 Tab 切换；成功后 token 落 localStorage（由 AuthContext 负责），跳抢单大厅。
 * mock 模式下表单预填演示账号，录屏时可以直接点「登录」。
 */

import { useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';

import { isMockMode } from '../api/client';
import { toErrorMessage } from '../api/errors';
import { MOCK_DEMO_ACCOUNTS } from '../api/mock';
import { useToast } from '../components/Toast';
import { Button, Card, Field, TextInput } from '../components/ui';
import { useAuth } from '../state/AuthContext';

type Tab = 'login' | 'register';

/** 演示账号：与 `mock.ts` 单一来源，避免提示文案与种子数据漂移。 */
const DEMO_PRIMARY = MOCK_DEMO_ACCOUNTS[0];
const DEMO_SECONDARY = MOCK_DEMO_ACCOUNTS[1];

/** 顶部品牌行：需求要求「一行显眼的文字」。 */
function Brand(): JSX.Element {
  return (
    <div className="mb-6 text-center">
      <h1 className="text-2xl font-bold tracking-tight text-gray-900">CampusRun · 校园跑腿</h1>
      <p className="mt-2 text-sm text-gray-500">帮取快递 · 代买跑腿，顺路赚点零花</p>
    </div>
  );
}

/** Tab 按钮。 */
function TabButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: string;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex-1 rounded-lg py-2 text-sm font-medium ${
        active ? 'bg-white text-primary shadow-sm' : 'text-gray-500'
      }`}
    >
      {children}
    </button>
  );
}

/** 登录/注册页。 */
export function LoginPage(): JSX.Element {
  const { authenticated, login, register } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();

  const [tab, setTab] = useState<Tab>('login');
  const [nickname, setNickname] = useState<string>('');
  const [phone, setPhone] = useState<string>(isMockMode ? DEMO_PRIMARY.phone : '');
  const [password, setPassword] = useState<string>(isMockMode ? DEMO_PRIMARY.password : '');
  const [submitting, setSubmitting] = useState<boolean>(false);

  if (authenticated) {
    return <Navigate to="/hall" replace />;
  }

  const switchTab = (next: Tab): void => {
    setTab(next);
  };

  const handleSubmit = async (): Promise<void> => {
    if (submitting) {
      return;
    }
    if (phone.trim() === '' || password === '') {
      toast.error('请填写手机号和密码');
      return;
    }
    if (tab === 'register' && nickname.trim() === '') {
      toast.error('请填写昵称');
      return;
    }

    setSubmitting(true);
    try {
      if (tab === 'login') {
        const user = await login(phone.trim(), password);
        toast.success(`欢迎回来，${user.nickname}`);
      } else {
        const user = await register(nickname.trim(), phone.trim(), password);
        toast.success(`注册成功，${user.nickname}`);
      }
      navigate('/hall', { replace: true });
    } catch (error) {
      toast.error(toErrorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-100">
      <div className="mx-auto flex min-h-screen max-w-app flex-col justify-center px-5 py-10">
        <Brand />

        <Card className="p-5">
          <div className="mb-5 flex gap-1 rounded-lg bg-gray-100 p-1">
            <TabButton active={tab === 'login'} onClick={() => switchTab('login')}>
              登录
            </TabButton>
            <TabButton active={tab === 'register'} onClick={() => switchTab('register')}>
              注册
            </TabButton>
          </div>

          <div className="space-y-4">
            {tab === 'register' ? (
              <Field label="昵称" required htmlFor="nickname">
                <TextInput
                  id="nickname"
                  value={nickname}
                  placeholder="2–16 位，同学都这么叫你"
                  maxLength={16}
                  autoComplete="nickname"
                  onChange={setNickname}
                />
              </Field>
            ) : null}

            <Field label="手机号" required htmlFor="phone">
              <TextInput
                id="phone"
                value={phone}
                placeholder="11 位手机号"
                inputMode="numeric"
                maxLength={11}
                autoComplete="tel"
                onChange={setPhone}
              />
            </Field>

            <Field label="密码" required htmlFor="password" hint={tab === 'register' ? '至少 6 位' : undefined}>
              <TextInput
                id="password"
                type="password"
                value={password}
                placeholder="请输入密码"
                autoComplete={tab === 'login' ? 'current-password' : 'new-password'}
                onChange={setPassword}
              />
            </Field>

            <Button block size="lg" loading={submitting} onClick={() => void handleSubmit()}>
              {tab === 'login' ? '登录' : '注册并登录'}
            </Button>
          </div>
        </Card>

        {isMockMode ? (
          <div className="mt-4 rounded-lg border border-blue-100 bg-primary-light px-4 py-3 text-xs leading-relaxed text-blue-700">
            <p className="font-medium">当前为 Mock 模式（未连接后端）</p>
            <p className="mt-1">
              演示账号：<span className="font-mono">{DEMO_PRIMARY.phone} / {DEMO_PRIMARY.password}</span>
              （{DEMO_PRIMARY.label}）
            </p>
            <p>
              另一个账号：<span className="font-mono">{DEMO_SECONDARY.phone} / {DEMO_SECONDARY.password}</span>
              （{DEMO_SECONDARY.label}）
            </p>
          </div>
        ) : null}

        <p className="mt-8 text-center text-xs text-gray-400">CampusRun · 校园内的顺路互助</p>
      </div>
    </div>
  );
}
