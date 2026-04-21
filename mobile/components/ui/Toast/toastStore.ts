import { create } from 'zustand'

export type ToastVariant = 'info' | 'success' | 'warning' | 'error'

export interface ToastItem {
	id: number
	message: string
	variant: ToastVariant
	duration: number
}

interface ToastStore {
	toasts: ToastItem[]
	addToast: (message: string, variant: ToastVariant, duration: number) => void
	removeToast: (id: number) => void
}

let nextId = 1

export const useToastStore = create<ToastStore>((set) => ({
	toasts: [],
	addToast: (message, variant, duration) => {
		set((state) => ({
			toasts: [...state.toasts, { id: nextId++, message, variant, duration }],
		}))
	},
	removeToast: (id) => {
		set((state) => ({
			toasts: state.toasts.filter((t) => t.id !== id),
		}))
	},
}))

export const toast = {
	show: (message: string, variant: ToastVariant = 'info', duration: number = 3000) => {
		useToastStore.getState().addToast(message, variant, duration)
	},
}
