// Tiny jsdom driver shared by the Plans tests (no @testing-library in this app).
import { act, createElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

let host: HTMLElement;
let root: Root;

export function setup() {
  host = document.createElement('div');
  document.body.appendChild(host);
  act(() => { root = createRoot(host); });
}
export function teardown() {
  act(() => root.unmount());
  host.remove();
  document.body.innerHTML = '';
}
export function mount(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  act(() => root.render(createElement(MemoryRouter, null, createElement(QueryClientProvider, { client: qc }, node))));
}
/** The whole document — modals render in place, but query from the top to be safe. */
export const doc = () => document.body;
export function click(el: Element | null | undefined) {
  if (!el) throw new Error('element to click not found');
  act(() => {
    (el as HTMLElement).dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
  });
}
export function type(el: HTMLTextAreaElement | HTMLInputElement, value: string) {
  const proto = el instanceof window.HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
}
export const button = (text: string) =>
  Array.from(document.body.querySelectorAll('button')).find((b) => b.textContent?.trim() === text) ?? null;
export const flush = () => act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); });
