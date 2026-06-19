import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import type { Component, ComponentProps } from 'svelte';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Re-export types used by shadcn-svelte components
export type WithElementRef<T, E extends Element = Element> = T & {
  ref?: E | null;
};

export type WithoutChildrenOrChild<T> = Omit<T, 'children' | 'child'>;

export type WithoutChildren<T> = Omit<T, 'children'>;

export type WithChild<
  T,
  TChild = Record<never, never>,
> = Omit<T, 'child' | 'children'> & {
  child?: Component<TChild>;
  children?: never;
};

export type WithChildren<T = Record<never, never>> = T & {
  children?: import('svelte').Snippet;
};
