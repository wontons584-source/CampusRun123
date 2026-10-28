/**
 * 手写小组件集合（不引任何 UI 组件库）。
 *
 * 只保留录屏演示真正需要的 6 个：Button / Card / Badge / Field / TextInput / Select，
 * 外加 EmptyState 与 Skeleton 两个轻量状态占位。样式全部走 Tailwind，无动画。
 */

import type { ButtonHTMLAttributes, ChangeEvent, InputHTMLAttributes, ReactNode } from 'react';

// ---------------------------------------------------------------------------
// Button
// ---------------------------------------------------------------------------

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  block?: boolean;
  loading?: boolean;
  children: ReactNode;
}

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-white active:bg-primary-dark disabled:bg-blue-300',
  secondary: 'border border-gray-300 bg-white text-gray-800 active:bg-gray-100 disabled:text-gray-400',
  ghost: 'bg-transparent text-primary active:bg-primary-light disabled:text-gray-400',
  danger: 'bg-red-600 text-white active:bg-red-700 disabled:bg-red-300',
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-sm',
  md: 'h-10 px-4 text-sm',
  lg: 'h-12 px-5 text-base',
};

/** 统一按钮：录屏时点按反馈稳定，不做缩放动画。 */
export function Button(props: ButtonProps): JSX.Element {
  const {
    variant = 'primary',
    size = 'md',
    block = false,
    loading = false,
    disabled = false,
    children,
    type = 'button',
    ...rest
  } = props;

  const classes = [
    'inline-flex items-center justify-center rounded-lg font-medium',
    'transition-none focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
    VARIANT_CLASS[variant],
    SIZE_CLASS[size],
    block ? 'w-full' : '',
  ]
    .filter((item) => item !== '')
    .join(' ');

  return (
    <button
      {...rest}
      type={type}
      disabled={disabled || loading}
      className={classes}
    >
      {loading ? '处理中…' : children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

interface CardProps {
  children: ReactNode;
  className?: string;
  onClick?: () => void;
}

/** 白底圆角卡片：页面里所有内容块的统一容器。 */
export function Card({ children, className = '', onClick }: CardProps): JSX.Element {
  const interactive = typeof onClick === 'function';
  return (
    <div
      onClick={onClick}
      className={`rounded-xl border border-gray-100 bg-white p-4 shadow-sm ${
        interactive ? 'cursor-pointer active:bg-gray-50' : ''
      } ${className}`}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

export type BadgeTone = 'blue' | 'orange' | 'green' | 'gray' | 'red';

interface BadgeProps {
  tone: BadgeTone;
  children: ReactNode;
}

const BADGE_CLASS: Record<BadgeTone, string> = {
  blue: 'bg-blue-50 text-blue-700',
  orange: 'bg-orange-50 text-orange-700',
  green: 'bg-green-50 text-green-700',
  gray: 'bg-gray-100 text-gray-600',
  red: 'bg-red-50 text-red-700',
};

/** 语义色状态徽章。 */
export function Badge({ tone, children }: BadgeProps): JSX.Element {
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ${BADGE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Field（label + 控件 + hint）
// ---------------------------------------------------------------------------

interface FieldProps {
  label: string;
  required?: boolean;
  hint?: string;
  htmlFor?: string;
  children: ReactNode;
}

/** 表单行容器：label 在上、控件在下、hint 收尾。 */
export function Field({ label, required = false, hint, htmlFor, children }: FieldProps): JSX.Element {
  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="block text-sm font-medium text-gray-700">
        {label}
        {required ? <span className="ml-1 text-red-500">*</span> : null}
      </label>
      {children}
      {hint !== undefined && hint !== '' ? (
        <p className="text-xs text-gray-400">{hint}</p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TextInput
// ---------------------------------------------------------------------------

interface TextInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'className' | 'onChange'> {
  invalid?: boolean;
  onChange?: (value: string) => void;
}

/** 受控输入框；`onChange` 直接回调字符串，省掉每处 `e.target.value`。 */
export function TextInput(props: TextInputProps): JSX.Element {
  const { invalid = false, onChange, ...rest } = props;
  const border = invalid ? 'border-red-400' : 'border-gray-300';
  return (
    <input
      {...rest}
      onChange={(event: ChangeEvent<HTMLInputElement>) => {
        onChange?.(event.target.value);
      }}
      className={`h-11 w-full rounded-lg border ${border} bg-white px-3 text-base text-gray-900 placeholder:text-gray-400 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20`}
    />
  );
}

// ---------------------------------------------------------------------------
// Select
// ---------------------------------------------------------------------------

interface SelectOption {
  value: string;
  label: string;
}

interface SelectProps {
  id?: string;
  value: string;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}

/** 原生 select —— 移动端体验最好，也不用自己实现下拉弹层。 */
export function Select({ id, value, options, placeholder, disabled = false, onChange }: SelectProps): JSX.Element {
  return (
    <div className="relative">
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(event: ChangeEvent<HTMLSelectElement>) => {
          onChange(event.target.value);
        }}
        className="h-11 w-full appearance-none rounded-lg border border-gray-300 bg-white pl-3 pr-9 text-base text-gray-900 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:bg-gray-50 disabled:text-gray-400"
      >
        {placeholder !== undefined ? <option value="">{placeholder}</option> : null}
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <svg
        viewBox="0 0 20 20"
        aria-hidden="true"
        className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
      >
        <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// EmptyState / Skeleton
// ---------------------------------------------------------------------------

/** 空列表占位。 */
export function EmptyState({ title, description }: { title: string; description?: string }): JSX.Element {
  return (
    <div className="rounded-xl border border-dashed border-gray-200 bg-white px-4 py-12 text-center">
      <p className="text-sm font-medium text-gray-500">{title}</p>
      {description !== undefined && description !== '' ? (
        <p className="mt-1 text-xs text-gray-400">{description}</p>
      ) : null}
    </div>
  );
}

/** 骨架屏（比「加载中…」更像成品，且不产生动画）。 */
export function SkeletonList({ rows = 3 }: { rows?: number }): JSX.Element {
  return (
    <div className="space-y-3" aria-busy="true" aria-label="加载中">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm">
          <div className="h-4 w-1/2 rounded bg-gray-100" />
          <div className="mt-3 h-3 w-3/4 rounded bg-gray-100" />
          <div className="mt-4 flex items-center justify-between">
            <div className="h-3 w-16 rounded bg-gray-100" />
            <div className="h-8 w-20 rounded-lg bg-gray-100" />
          </div>
        </div>
      ))}
    </div>
  );
}

/** 居中一行加载提示（详情页用）。 */
export function LoadingHint({ text = '加载中…' }: { text?: string }): JSX.Element {
  return (
    <div className="flex items-center justify-center py-16 text-sm text-gray-400" role="status">
      {text}
    </div>
  );
}
