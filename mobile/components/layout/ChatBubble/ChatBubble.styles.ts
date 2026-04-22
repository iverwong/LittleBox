import { StyleSheet } from 'react-native'
import type { Theme } from '@/theme'

type ChatBubbleStyles = Record<string, object>

export const createStyles = (_theme: Theme): ChatBubbleStyles => {
	return StyleSheet.create({
		wrap: {
			display: 'flex',
			marginVertical: 4,
		},
		wrapUser: {
			alignSelf: 'flex-end',
		},
		wrapAi: {
			alignSelf: 'flex-start',
		},
		bubble: {
			maxWidth: '78%',
			paddingVertical: 10,
			paddingHorizontal: 14,
			borderRadius: 18,
			shadowColor: _theme.shadow.sm.shadowColor,
			shadowOffset: _theme.shadow.sm.shadowOffset,
			shadowOpacity: _theme.shadow.sm.shadowOpacity,
			shadowRadius: _theme.shadow.sm.shadowRadius,
			elevation: 1,
		},
		bubbleUser: {
			backgroundColor: _theme.palette.primary[500],
			borderBottomRightRadius: 4,
		},
		bubbleAi: {
			backgroundColor: _theme.surface.paper,
			borderBottomLeftRadius: 4,
		},
		text: {
			fontSize: _theme.typography.fontSize.base,
			lineHeight: _theme.typography.fontSize.base * _theme.typography.lineHeight.normal,
		},
		textUser: {
			color: '#FFFFFF',
		},
		textAi: {
			color: _theme.palette.neutral[800],
		},
	})
}
