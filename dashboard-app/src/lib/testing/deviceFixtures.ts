// Answers of the /a2ui/devices routes in the shapes the server sends, for the Devices tests. Every value is made up.

import type { Device, DeviceList, Pairing } from '../devices';

let next = 0;

/** One device row; `id` defaults to a new made-up UUID. */
export function deviceRow(over: Partial<Device> = {}): Device {
  next += 1;
  return {
    id: `00000000-0000-4000-8000-${String(next).padStart(12, '0')}`,
    name: 'Pixel 7',
    created_at: `${new Date().getUTCFullYear()}-11-12T09:00:00Z`, // this year: another year's day is printed with its year
    last_seen_at: null,
    revoked_at: null,
    ...over,
  };
}

/** A list answer: no device, no pairing since the server started, and redeeming not held back. */
export function deviceList(over: { devices?: Device[]; pairing?: Pairing; limited?: boolean } = {}): DeviceList {
  return { devices: over.devices ?? [], pairing: over.pairing ?? null, redeem: { limited: over.limited ?? false } };
}

export const OPEN = (tries_left = 5, expires_in = 300): Pairing => ({ state: 'open', expires_in, tries_left });
